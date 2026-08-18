"""
Silver layer — reads unprocessed rows from bronze_listings, flattens the
raw hit shape into proper columns, drops rows that can't be used
(missing price/area), and upserts into silver_listings keyed by
external_id — so each AD has exactly one row holding its latest state.

(Note: this dedupes by external_id — i.e. one row per AD. Matching
different ADS that represent the same physical property happens later,
in the Gold layer's resolver.)
"""
from datetime import datetime

from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert

from src.db import SessionLocal, BronzeListing, SilverListing
from src.monitoring import track_stage, new_run_id
from src.config import get_settings
from src.logging_config import get_logger
from src.retry import with_db_retry

log = get_logger(__name__)

settings = get_settings()

# Rows are paged through by id instead of pulled in one SELECT. bronze rows
# carry the full raw hit in a JSONB column, so a single unpaged
# SELECT over 50k rows can transfer hundreds of MB — over a real-latency
# link to a remote Supabase pooler that easily blows the statement_timeout,
# even though the query itself (an indexed boolean filter) is cheap on the
# DB side. Paging keeps each round trip small and bounded.
PAGE_SIZE = 5000
UPSERT_BATCH_SIZE = 1000


def build_listing_url(external_id: str | None, template: str) -> str | None:
    """Build a listing's public URL from its external id and the configured
    URL template (LISTING_URL_TEMPLATE in .env). Returns None when either is
    missing — the column stays NULL instead of writing a broken link.
    Pure function, no settings access — trivially unit-testable."""
    if not external_id or not template:
        return None
    return template.replace("{external_id}", external_id)


def _location_field(location: list, level_type: str) -> str | None:
    if not location:
        return None
    for loc in location:
        if loc.get("type") == level_type:
            return loc.get("name")
    return None


def _flatten(hit: dict, scraped_at: datetime) -> dict:
    location = hit.get("location") or []
    category = hit.get("category") or []
    geography = hit.get("geography") or {}
    agency = hit.get("agency") or {}
    external_id = str(hit.get("externalID") or hit.get("id"))

    return {
        "external_id": external_id,
        "url": build_listing_url(external_id, settings.listing_url_template),
        "title": hit.get("title"),
        "purpose": hit.get("purpose"),
        "price_egp": hit.get("price"),
        "down_payment_egp": hit.get("downPayment"),
        "rooms": hit.get("rooms"),
        "baths": hit.get("baths"),
        "area_m2": hit.get("area"),
        "plot_area_m2": hit.get("plotArea"),
        "property_type": category[-1]["name"] if category else None,
        "city": _location_field(location, "city"),
        "neighbourhood": _location_field(location, "neighbourhood"),
        "province": _location_field(location, "province"),
        "lat": geography.get("lat"),
        "lng": geography.get("lng"),
        "furnishing_status": hit.get("furnishingStatus"),
        "completion_status": hit.get("completionStatus"),
        "agency_name": agency.get("name"),
        "photo_count": hit.get("photoCount"),
        "scraped_at": scraped_at,
    }


def _rejection_reason(row: dict) -> str | None:
    """Returns None if valid, otherwise a short reason string for monitoring.

    Every distinct reason string shows up in pipeline_runs.stats.dropped_reasons,
    so adding a new check here automatically adds a new counter in the
    monitoring dashboard — no extra wiring needed.
    """
    price = row.get("price_egp")
    area = row.get("area_m2")
    has_price = bool(price) and price > 0
    has_area = bool(area) and area > 0

    # --- Presence checks (original) ---
    if not has_price and not has_area:
        return "missing_price_and_area"
    if not has_price:
        return "missing_price"
    if not has_area:
        return "missing_area"

    # --- Price sanity (Egyptian real estate context) ---
    # Below 10K EGP is almost certainly a test listing or data error;
    # above 500M EGP is implausible for a single residential unit.
    if price < 10_000:
        return "price_suspiciously_low"
    if price > 500_000_000:
        return "price_suspiciously_high"

    # --- Area sanity ---
    if area > 10_000:
        return "area_suspiciously_large"

    # --- Room count ---
    rooms = row.get("rooms")
    if rooms is not None:
        if rooms < 0:
            return "negative_rooms"
        if rooms > 20:
            return "rooms_suspiciously_high"

    # --- Coordinates in Egypt bounding box (when present) ---
    # Rough bounds: lat 22.0–31.7, lng 24.7–36.9.  Listings outside
    # this box are data errors (GPS glitches, test data, etc.).
    lat, lng = row.get("lat"), row.get("lng")
    if lat is not None and lng is not None:
        if not (22.0 <= lat <= 31.7 and 24.7 <= lng <= 36.9):
            return "coordinates_outside_egypt"

    return None


@with_db_retry
def transform_silver(run_id: str | None = None) -> dict:
    run_id = run_id or new_run_id()
    stats = {"pending": 0, "upserted": 0, "dropped": 0, "duplicates_collapsed": 0, "dropped_reasons": {}}

    reasons: dict[str, int] = {}

    with track_stage(run_id, "silver") as tracked:
        with SessionLocal() as session:
            total_pending = session.execute(
                select(func.count()).select_from(BronzeListing).where(BronzeListing.processed.is_(False))
            ).scalar_one()
            stats["pending"] = total_pending
            log.info(f"{total_pending} unprocessed bronze rows found")

            if not total_pending:
                tracked.update(stats)
                return stats

            last_id = 0
            while True:
                page = session.execute(
                    select(BronzeListing)
                    .where(BronzeListing.processed.is_(False), BronzeListing.id > last_id)
                    .order_by(BronzeListing.id)
                    .limit(PAGE_SIZE)
                ).scalars().all()
                if not page:
                    break
                last_id = page[-1].id

                # Keep only the latest scrape per external_id within this page.
                # Cross-page duplicates are handled below by the conditional
                # ON CONFLICT (only overwrite silver if the new row is newer),
                # so this stays correct regardless of page processing order.
                latest_by_id: dict[str, BronzeListing] = {}
                for row in page:
                    existing = latest_by_id.get(row.external_id)
                    if existing is None or row.scraped_at > existing.scraped_at:
                        latest_by_id[row.external_id] = row
                stats["duplicates_collapsed"] += len(page) - len(latest_by_id)

                rows_to_upsert: list[dict] = []
                for external_id, bronze_row in latest_by_id.items():
                    flat = _flatten(bronze_row.raw_data, bronze_row.scraped_at)
                    reason = _rejection_reason(flat)
                    if reason:
                        reasons[reason] = reasons.get(reason, 0) + 1
                        stats["dropped"] += 1
                        continue

                    rows_to_upsert.append(flat)
                    stats["upserted"] += 1

                # Batch the upserts instead of one execute() per row — cuts
                # network round trips from one-per-row to one per
                # UPSERT_BATCH_SIZE rows.
                for i in range(0, len(rows_to_upsert), UPSERT_BATCH_SIZE):
                    chunk = rows_to_upsert[i:i + UPSERT_BATCH_SIZE]
                    stmt = insert(SilverListing).values(chunk)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["external_id"],
                        set_={
                            k: stmt.excluded[k]
                            for k in chunk[0]
                            if k != "external_id"
                        },
                        # Only overwrite if this row is actually newer, so
                        # results don't depend on the order pages are processed in.
                        where=(SilverListing.scraped_at < stmt.excluded.scraped_at),
                    )
                    session.execute(stmt)

                page_ids = [row.id for row in page]
                session.execute(
                    update(BronzeListing)
                    .where(BronzeListing.id.in_(page_ids))
                    .values(processed=True)
                )
                session.commit()

            stats["dropped_reasons"] = reasons
            tracked.update(stats)
            log.info(f"Silver: {stats}")
    return stats


if __name__ == "__main__":
    transform_silver()
"""
Gold layer — reads all Silver listings and, for each one:
  1. Finds or creates its Location (city + neighbourhood)
  2. Resolves it to a canonical Property via src/gold/matcher.py
  3. Upserts its Listing (one ad, pointing at that Property)
  4. Appends to PriceHistory if the price is new or changed
Then recomputes price_per_m2 + outlier flags across all Properties.

Records duplicate counts, rejection counts, and timing via src/monitoring.py.
"""
from collections import defaultdict

import pandas as pd
from sqlalchemy import select, update, values, column, Integer, Float, Boolean
from sqlalchemy.orm import selectinload

from src.db import SessionLocal, SilverListing, Location, Property, Listing, PriceHistory
from src.gold.matcher import find_match, extract_title_signals, aggregate_signals
from src.monitoring import track_stage, new_run_id
from src.logging_config import get_logger
from src.retry import with_db_retry

log = get_logger(__name__)

FLUSH_EVERY = 2000  # periodic flush so new rows get ids, without one round trip per row
UPDATE_BATCH_SIZE = 2000


def _bulk_update_properties(session, rows: list[dict], columns: list[tuple[str, type]], chunk_size: int = UPDATE_BATCH_SIZE):
    """Bulk-write columns onto existing Property rows via UPDATE ... FROM (VALUES ...),
    chunked, instead of setting attributes on each ORM object and letting commit()
    flush one UPDATE per row.
    """
    if not rows:
        return
    col_defs = [column("id", Integer)] + [column(name, sa_type) for name, sa_type in columns]
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        data = [tuple([r["id"]] + [r[name] for name, _ in columns]) for r in chunk]
        v = values(*col_defs, name="v").data(data)
        stmt = update(Property).where(Property.id == v.c.id).values(
            **{name: getattr(v.c, name) for name, _ in columns}
        )
        session.execute(stmt)


def _candidate_dict(key, p: Property, price: float = None,
                     signals=None) -> dict:
    """
    Convert a Property ORM object into a plain dict for the matcher.

    Includes aggregated title signals (building/phase/view/floor) from ALL
    of the property's listings, not just one — so a hard conflict fires if
    the new ad disagrees with ANY existing ad on the property (v4 fix).

    Args:
        key: Synthetic or real Property ID
        p: Property ORM object
        price: Optional override price (for unflushed new Properties)
        signals: Optional AggregatedTitleSignals built from all listings

    Returns:
        Dict with all fields needed by find_match()
    """
    return {
        "id": key,
        "property_type": p.property_type,
        "rooms": p.rooms,
        "baths": p.baths,
        "area_m2": p.area_m2,
        "lat": p.lat,
        "lng": p.lng,
        # Existing properties carry current_price_egp from the last
        # aggregate run; brand-new ones (created below, mid-loop) pass
        # their originating listing's price explicitly since
        # current_price_egp isn't computed until the aggregate stage.
        "price_egp": price if price is not None else p.current_price_egp,
        # Aggregated signals from ALL listings — the matcher uses these for
        # hard conflict detection (building/phase/view/floor).
        "signals": signals,
    }


def _split_over_merged_properties(session) -> int:
    """Repair historical over-merges: properties whose listings reference
    multiple distinct buildings/phases (e.g. B10+B11+B12+B14 in Madinaty)
    get split into one Property per (building, phase) group.

    Returns the number of properties split.
    """
    from sqlalchemy.orm import selectinload
    properties = session.execute(
        select(Property).options(selectinload(Property.listings))
    ).scalars().all()

    split_count = 0
    for p in properties:
        if len(p.listings) < 2:
            continue

        # Group listings by (building, phase) extracted from their titles
        groups: dict[tuple, list[Listing]] = defaultdict(list)
        for l in p.listings:
            sigs = extract_title_signals(l.title)
            key = (sigs.building or "", sigs.phase or "")
            groups[key].append(l)

        # If all listings share the same (building, phase), no split needed
        if len(groups) <= 1:
            continue

        # Split: keep the largest group on the original property, create
        # new properties for the rest.
        sorted_groups = sorted(groups.values(), key=len, reverse=True)
        for listings in sorted_groups[1:]:
            new_p = Property(
                location_id=p.location_id,
                property_type=p.property_type,
                rooms=p.rooms, baths=p.baths, area_m2=p.area_m2,
                lat=p.lat, lng=p.lng,
                match_method="split", match_confidence=1.0,
            )
            session.add(new_p)
            session.flush()  # need new_p.id now
            for l in listings:
                l.property = new_p
            split_count += 1

    if split_count:
        session.commit()
    return split_count


@with_db_retry
def resolve_listings(run_id: str, force: bool = False) -> dict:
    """Stage: Silver -> Property + Listing + PriceHistory.

    force=True bypasses the incremental skip and re-matches ALL listings —
    use after a matcher upgrade to repair existing data with the new gates.

    This used to query the DB 3-4 times per Silver row (location lookup,
    candidate lookup, listing lookup, plus a flush after every new
    Property/Listing to get its generated id) — a classic N+1 that turned
    ~48,000 rows into 150,000+ individual round trips to a remote Supabase
    pooler. Everything knowable up front is now preloaded once into
    in-memory dicts, and new rows are linked via ORM relationships
    (listing.property = property_obj) instead of raw FK ids, so SQLAlchemy
    resolves ids at flush time instead of forcing an immediate round trip
    per row.
    """
    stats = {"processed": 0, "new_properties": 0, "matched_existing": 0,
              "duplicates_by_method": defaultdict(int), "skipped_no_location": 0}

    with track_stage(run_id, "gold_resolve") as tracked:
        with SessionLocal() as session:
            # Repair historical over-merges first, so new ads in this run
            # match against the corrected (split) properties, not the
            # polluted originals.
            split_count = _split_over_merged_properties(session)
            if split_count:
                log.info(f"[{run_id}] Split {split_count} over-merged properties")

            silver_rows = session.execute(select(SilverListing)).scalars().all()

            # --- Preload everything the loop used to re-query per row ---
            locations_by_key: dict[tuple, Location] = {
                (loc.city, loc.neighbourhood): loc
                for loc in session.execute(select(Location)).scalars()
            }

            # candidates_by_location holds plain dicts (what the matcher
            # expects); local_key_to_property maps each candidate's "id" back
            # to the real ORM object, so a brand-new (unflushed, id=None)
            # Property created mid-loop can still be matched against later
            # rows in the same run without a DB round trip.
            # 
            # IMPROVEMENT: Index by (location_id, property_type, rooms) instead of
            # just location_id. This dramatically reduces unnecessary comparisons
            # when there are many properties in the same neighbourhood with different
            # specs (e.g., 2BR vs 3BR apartments). The matcher already filters on
            # these fields, so pre-filtering here is a performance optimization.
            local_key_to_property: dict[object, Property] = {}
            candidates_by_index: dict[tuple, list[dict]] = defaultdict(list)
            
            # Preload existing Properties with their first listing title for disambiguation
            # Join Property with its first Listing to get a representative title
            from sqlalchemy.orm import joinedload
            properties_with_listings = session.execute(
                select(Property)
                .options(joinedload(Property.listings))
            ).scalars()
            
            for p in properties_with_listings:
                local_key_to_property[p.id] = p
                # Aggregate title signals from ALL listings (v4 fix) so a hard
                # conflict fires if the new ad disagrees with ANY existing ad
                # on the property — not just the first listing's title.
                signals = aggregate_signals(
                    [extract_title_signals(l.title) for l in p.listings if l.title]
                ) if p.listings else None
                index_key = (p.location_id, p.property_type, p.rooms)
                candidates_by_index[index_key].append(_candidate_dict(p.id, p, signals=signals))

            listings_by_external_id: dict[str, Listing] = {
                l.external_id: l for l in session.execute(select(Listing)).scalars()
            }

            def get_or_create_location(city, neighbourhood, province) -> Location:
                key = (city, neighbourhood)
                loc = locations_by_key.get(key)
                if loc:
                    return loc
                loc = Location(city=city, neighbourhood=neighbourhood, province=province)
                session.add(loc)
                session.flush()  # need loc.id now — new distinct locations are rare (dozens, not 48k)
                locations_by_key[key] = loc
                return loc

            pending_new_keys = []  # synthetic keys awaiting a real id from the next flush

            for i, row in enumerate(silver_rows):
                stats["processed"] += 1

                if not row.city:
                    stats["skipped_no_location"] += 1
                    continue

                location = get_or_create_location(row.city, row.neighbourhood, row.province)

                # Retrieve candidates using the improved index key
                # This filters to only properties with matching (location, type, rooms)
                index_key = (location.id, row.property_type, row.rooms)
                candidates = candidates_by_index[index_key]
                
                new_listing_attrs = {
                    "property_type": row.property_type, 
                    "rooms": row.rooms,
                    "baths": row.baths,
                    "area_m2": row.area_m2, 
                    "lat": row.lat, 
                    "lng": row.lng,
                    "price_egp": row.price_egp,
                    "title": row.title,  # Critical for phase/building extraction
                }
                match = find_match(new_listing_attrs, candidates)

                if match.property_id is not None:
                    property_obj = local_key_to_property[match.property_id]
                    stats["matched_existing"] += 1
                    stats["duplicates_by_method"][match.method] += 1
                    # Update the candidate's aggregated signals to include this
                    # new listing's signals — so later rows in the same run
                    # check against ALL listings, not just the original ones.
                    new_sigs = extract_title_signals(row.title)
                    for c in candidates:
                        if c.get("id") == match.property_id and c.get("signals") is not None:
                            agg = c["signals"]
                            if new_sigs.floor_type:
                                agg.floor_types.add(new_sigs.floor_type)
                            if new_sigs.view_type:
                                agg.view_types.add(new_sigs.view_type)
                            if new_sigs.phase:
                                agg.phases.add(new_sigs.phase)
                            if new_sigs.building:
                                agg.buildings.add(new_sigs.building)
                            if new_sigs.has_garden:
                                agg.has_garden = True
                            if new_sigs.is_ground:
                                agg.is_ground = True
                            break
                else:
                    property_obj = Property(
                        location_id=location.id,
                        property_type=row.property_type, rooms=row.rooms, baths=row.baths,
                        area_m2=row.area_m2, lat=row.lat, lng=row.lng,
                        match_method="new", match_confidence=1.0,
                    )
                    session.add(property_obj)
                    synth_key = object()  # placeholder until flush assigns a real id
                    local_key_to_property[synth_key] = property_obj
                    # New property: start with signals from this listing
                    new_signals = extract_title_signals(row.title)
                    agg = aggregate_signals([new_signals])
                    # Add to the index for later candidates in the same run
                    candidates_by_index[index_key].append(
                        _candidate_dict(synth_key, property_obj, price=row.price_egp, signals=agg)
                    )
                    pending_new_keys.append(synth_key)
                    stats["new_properties"] += 1

                # Upsert the Listing itself
                listing = listings_by_external_id.get(row.external_id)

                if listing is None:
                    listing = Listing(
                        external_id=row.external_id, property=property_obj,
                        url=row.url,
                        title=row.title, furnishing_status=row.furnishing_status,
                        completion_status=row.completion_status, agency_name=row.agency_name,
                        photo_count=row.photo_count, current_price_egp=row.price_egp,
                    )
                    session.add(listing)
                    listings_by_external_id[row.external_id] = listing
                    session.add(PriceHistory(listing=listing, price_egp=row.price_egp))
                else:
                    listing.last_seen_at = row.updated_at
                    listing.status = "active"
                    if row.url:
                        listing.url = row.url
                    if listing.current_price_egp != row.price_egp:
                        listing.current_price_egp = row.price_egp
                        session.add(PriceHistory(listing=listing, price_egp=row.price_egp))

                # Periodic flush so pending Properties get real ids (swapped
                # into local_key_to_property so later candidate lookups keep
                # working) — without paying a round trip on every single row.
                if (i + 1) % FLUSH_EVERY == 0 and pending_new_keys:
                    session.flush()
                    for key in pending_new_keys:
                        p = local_key_to_property[key]
                        local_key_to_property[p.id] = p
                    pending_new_keys = []

            # --- Stale marking ------------------------------------------------
            # Silver holds the latest state per ad, so a Listing whose
            # external_id has no Silver row anymore is an ad absent from the
            # latest scrape — mark it stale (bulk UPDATE, not per-row ORM
            # flushes). Aggregates only consider active listings, so a
            # delisted ad stops dragging its property's numbers down. Ads that
            # come back are reactivated in the loop above.
            seen_external_ids = {r.external_id for r in silver_rows}
            stale_ids = [
                l.id for l in listings_by_external_id.values()
                if l.external_id not in seen_external_ids
            ]
            if stale_ids:
                for i in range(0, len(stale_ids), UPDATE_BATCH_SIZE):
                    session.execute(
                        update(Listing)
                        .where(Listing.id.in_(stale_ids[i:i + UPDATE_BATCH_SIZE]))
                        .values(status="stale")
                    )
                log.info(f"[{run_id}] Marked {len(stale_ids)} listings stale (absent from latest scrape)")

            session.commit()

        tracked.update({
            "processed": stats["processed"],
            "new_properties": stats["new_properties"],
            "matched_existing_as_duplicate_ads": stats["matched_existing"],
            "duplicate_match_methods": dict(stats["duplicates_by_method"]),
            "skipped_missing_location": stats["skipped_no_location"],
        })
    return stats


@with_db_retry
def compute_aggregates(run_id: str) -> int:
    """Stage: recompute price_per_m2 + outlier flags across all Properties.

    Previously this set attributes on every Property ORM object and let
    commit() flush them — SQLAlchemy issues one UPDATE per dirty row by
    default, so 16k+ properties meant 16k+ individual round trips (this is
    what was taking ~23 minutes). Values are now computed into plain Python
    dicts and written back with a batched UPDATE ... FROM (VALUES ...), same
    pattern as the batched upserts used elsewhere in this pipeline.
    """
    with track_stage(run_id, "gold_aggregate") as tracked:
        with SessionLocal() as session:
            # selectinload(Property.listings) pulls every property's listings
            # in one extra batched query. Without it, each `p.listings` access
            # below lazy-loads on its own — one query per property (another
            # N+1: tens of thousands of properties = tens of thousands of
            # round trips).
            properties = session.execute(
                select(Property).options(selectinload(Property.listings))
            ).scalars().all()
            if not properties:
                tracked["updated"] = 0
                return 0

            # current_price_egp = cheapest ACTIVE listing's price — stale
            # (delisted) ads are excluded so they don't drag the property's
            # numbers down. Computed into a plain dict rather than set on the
            # ORM objects directly, so nothing gets marked dirty for an
            # implicit per-row flush.
            current_price_by_id: dict[int, float] = {}
            for p in properties:
                listing_prices = [l.current_price_egp for l in p.listings
                                  if l.status == "active" and l.current_price_egp]
                current_price_by_id[p.id] = min(listing_prices) if listing_prices else None

            df = pd.DataFrame([
                {"id": p.id, "price": current_price_by_id[p.id], "area_m2": p.area_m2}
                for p in properties
                if current_price_by_id[p.id] and p.area_m2
            ])

            price_per_m2_by_id: dict[int, float] = {}
            is_outlier_by_id: dict[int, bool] = {}
            if not df.empty:
                df["price_per_m2"] = df["price"] / df["area_m2"]
                q1, q99 = df["price_per_m2"].quantile([0.01, 0.99])
                df["is_outlier"] = (df["price_per_m2"] < q1) | (df["price_per_m2"] > q99)
                for row in df.itertuples():
                    price_per_m2_by_id[row.id] = row.price_per_m2
                    is_outlier_by_id[row.id] = bool(row.is_outlier)

            price_rows = [{"id": pid, "current_price_egp": cpe} for pid, cpe in current_price_by_id.items()]
            _bulk_update_properties(session, price_rows, [("current_price_egp", Float)])

            outlier_rows = [
                {"id": pid, "price_per_m2": price_per_m2_by_id[pid], "is_outlier": is_outlier_by_id[pid]}
                for pid in price_per_m2_by_id
            ]
            _bulk_update_properties(session, outlier_rows, [("price_per_m2", Float), ("is_outlier", Boolean)])

            session.commit()
            outliers_flagged = int(df["is_outlier"].sum()) if not df.empty else 0
            tracked.update({"updated": len(df), "outliers_flagged": outliers_flagged})
            return len(df)


def build_gold(force: bool = True) -> dict:
    run_id = new_run_id()
    resolve_stats = resolve_listings(run_id, force=force)
    updated = compute_aggregates(run_id)
    log.info(f"[{run_id}] Gold build complete — {resolve_stats['new_properties']} new properties, "
              f"{resolve_stats['matched_existing']} ads matched to existing properties, "
              f"{updated} properties re-aggregated")
    return resolve_stats


if __name__ == "__main__":
    build_gold()
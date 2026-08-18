"""
Feature Store — runs after Gold. One row per Property with the exact
features training/serving should use, so both read from the same place
(avoids training-serving skew). Plain Postgres table, not a separate
service — that's not needed at this scale.
"""
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select, Integer, Float, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column, selectinload

from src.db import Base, SessionLocal, Property, Location, migration_engine
from src.monitoring import track_stage, new_run_id
from src.logging_config import get_logger
from src.retry import with_db_retry

log = get_logger(__name__)

UPSERT_BATCH_SIZE = 1000


class PropertyFeatures(Base):
    __tablename__ = "property_features"

    property_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_type: Mapped[str] = mapped_column(String, nullable=True)
    rooms: Mapped[float] = mapped_column(Float, nullable=True)
    baths: Mapped[float] = mapped_column(Float, nullable=True)
    area_m2: Mapped[float] = mapped_column(Float, nullable=True)
    city: Mapped[str] = mapped_column(String, nullable=True)
    neighbourhood: Mapped[str] = mapped_column(String, nullable=True)
    province: Mapped[str] = mapped_column(String, nullable=True)
    price_egp: Mapped[float] = mapped_column(Float, nullable=True)
    price_per_m2: Mapped[float] = mapped_column(Float, nullable=True)
    is_outlier: Mapped[bool] = mapped_column(Boolean, default=False)

    # Unit & listing details from the newest active listing
    representative_title: Mapped[str] = mapped_column(String, nullable=True)
    furnishing_status: Mapped[str] = mapped_column(String, nullable=True)
    completion_status: Mapped[str] = mapped_column(String, nullable=True)
    url: Mapped[str] = mapped_column(String, nullable=True)
    agency_name: Mapped[str] = mapped_column(String, nullable=True)
    photo_count: Mapped[int] = mapped_column(Integer, nullable=True)

    # Derived / contextual features
    active_listing_count: Mapped[int] = mapped_column(Integer, default=0)   # how many ACTIVE ads point at this property
    neighbourhood_avg_price_per_m2: Mapped[float] = mapped_column(Float, nullable=True)
    days_on_market: Mapped[int] = mapped_column(Integer, nullable=True)

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


@with_db_retry
def build_features() -> dict:
    run_id = new_run_id()
    with track_stage(run_id, "feature_store") as tracked:
        # DDL goes through the direct/session connection — the transaction
        # pooler (used for `engine`/SessionLocal elsewhere) doesn't reliably
        # support CREATE TABLE inside pgbouncer's transaction mode.
        Base.metadata.create_all(migration_engine, tables=[PropertyFeatures.__table__])

        from sqlalchemy import text
        with migration_engine.connect() as conn:
            conn.execute(text("""
                ALTER TABLE property_features
                ADD COLUMN IF NOT EXISTS province VARCHAR,
                ADD COLUMN IF NOT EXISTS furnishing_status VARCHAR,
                ADD COLUMN IF NOT EXISTS completion_status VARCHAR,
                ADD COLUMN IF NOT EXISTS url VARCHAR,
                ADD COLUMN IF NOT EXISTS agency_name VARCHAR,
                ADD COLUMN IF NOT EXISTS photo_count INTEGER;
            """))
            conn.commit()

        with SessionLocal() as session:
            properties = session.execute(
                select(Property).options(selectinload(Property.listings))
            ).scalars().all()
            if not properties:
                tracked["computed"] = 0
                return {"computed": 0}

            # Preload all locations once — the old per-property
            # session.get(Location, ...) was another N+1 round trip.
            locations_by_id: dict[int, Location] = {
                loc.id: loc for loc in session.execute(select(Location)).scalars()
            }

            # Neighbourhood-level average price/m2, used as a contextual feature
            loc_prices: dict[int, list[float]] = defaultdict(list)
            for p in properties:
                if p.location_id and p.price_per_m2:
                    loc_prices[p.location_id].append(p.price_per_m2)
            neighbourhood_avgs: dict[int, float] = {
                loc_id: sum(prices) / len(prices) for loc_id, prices in loc_prices.items()
            }

            now = datetime.now(timezone.utc)
            records = []
            for p in properties:
                location = locations_by_id.get(p.location_id)
                active_listings = [l for l in p.listings if l.status == "active"]
                earliest_listing = min((l.first_seen_at for l in active_listings), default=None)
                days_on_market = (now - earliest_listing).days if earliest_listing else None
                newest_active = max(active_listings, key=lambda l: l.last_seen_at or l.first_seen_at, default=None)

                records.append({
                    "property_id": p.id,
                    "property_type": p.property_type, "rooms": p.rooms, "baths": p.baths,
                    "area_m2": p.area_m2,
                    "city": location.city if location else None,
                    "neighbourhood": location.neighbourhood if location else None,
                    "province": location.province if location else None,
                    "price_egp": p.current_price_egp, "price_per_m2": p.price_per_m2,
                    "is_outlier": p.is_outlier,
                    "representative_title": newest_active.title if newest_active else None,
                    "furnishing_status": newest_active.furnishing_status if newest_active else None,
                    "completion_status": newest_active.completion_status if newest_active else None,
                    "url": newest_active.url if newest_active else None,
                    "agency_name": newest_active.agency_name if newest_active else None,
                    "photo_count": newest_active.photo_count if newest_active else None,
                    "active_listing_count": len(active_listings),
                    "neighbourhood_avg_price_per_m2": neighbourhood_avgs.get(p.location_id),
                    "days_on_market": days_on_market,
                    "computed_at": now,
                })

            # Batch the upserts — one statement per chunk instead of one per
            # property (the old per-row execute loop was yet another N+1:
            # ~16k properties = ~16k round trips to the remote pooler).
            for i in range(0, len(records), UPSERT_BATCH_SIZE):
                chunk = records[i:i + UPSERT_BATCH_SIZE]
                stmt = insert(PropertyFeatures).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["property_id"],
                    set_={k: stmt.excluded[k] for k in chunk[0] if k != "property_id"},
                )
                session.execute(stmt)

            session.commit()
            tracked["computed"] = len(records)
            log.info(f"Feature store: computed features for {len(records)} properties")
            return {"computed": len(records)}


if __name__ == "__main__":
    build_features()

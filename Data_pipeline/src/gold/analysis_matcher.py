"""
Analysis matcher — maximum recall for dashboard analytics.

Groups ALL listings with the same (location, property_type, rooms) into one
stable analysis Property: `analysis_properties` rows persist across runs and
are keyed by a UNIQUE (location_id, property_type, rooms), so the IDs a
dashboard sees never collide between runs (the old per-run sequential IDs
restarted at 1 every run and silently meant different groups).

`property_analysis_matches` stores one row per (run, silver listing) with a
UNIQUE constraint, so re-running a pipeline upserts instead of growing the
table unboundedly.

For dashboard aggregates (avg price, listing counts, price trends) this
produces the most useful groupings.

Performance note: all AnalysisProperty and Location rows are batch-upserted
instead of flushed one-at-a-time, so the whole stage runs in a handful of
DB round trips rather than thousands — critical for surviving Supabase's
statement_timeout and PgBouncer connection recycling.
"""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.db import (
    SessionLocal, SilverListing, Location, AnalysisProperty, PropertyAnalysisMatch,
)
from src.monitoring import track_stage, new_run_id
from src.logging_config import get_logger
from src.retry import with_db_retry

log = get_logger(__name__)

UPSERT_BATCH_SIZE = 1000


@with_db_retry
def run_analysis_matcher(run_id: str) -> dict:
    """Stage 6: Silver -> stable analysis-property groupings for dashboard."""
    stats = {"processed": 0, "new_groups": 0, "merged": 0, "skipped_no_location": 0}

    with track_stage(run_id, "analysis_matcher") as tracked:
        with SessionLocal() as session:
            silver_rows = session.execute(select(SilverListing)).scalars().all()

            # --- Pre-load existing reference data --------------------------
            locations_by_key = {
                (loc.city, loc.neighbourhood): loc
                for loc in session.execute(select(Location)).scalars()
            }

            # --- Pass 1: collect all needed Locations ----------------------
            # Identify Location keys that don't exist yet and batch-insert
            # them, instead of flushing one at a time.
            new_location_rows: list[dict] = []
            seen_new_locations: set[tuple] = set()

            for row in silver_rows:
                if not row.city:
                    continue
                key = (row.city, row.neighbourhood)
                if key not in locations_by_key and key not in seen_new_locations:
                    new_location_rows.append({
                        "city": row.city,
                        "neighbourhood": row.neighbourhood,
                        "province": row.province,
                    })
                    seen_new_locations.add(key)

            # Batch-upsert new Locations (ON CONFLICT DO NOTHING — safe if
            # another run already created them).
            for i in range(0, len(new_location_rows), UPSERT_BATCH_SIZE):
                chunk = new_location_rows[i:i + UPSERT_BATCH_SIZE]
                stmt = insert(Location).values(chunk)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["city", "neighbourhood"],
                )
                session.execute(stmt)

            # Reload all locations so we have ids for any newly created ones.
            if new_location_rows:
                session.flush()
                locations_by_key = {
                    (loc.city, loc.neighbourhood): loc
                    for loc in session.execute(select(Location)).scalars()
                }

            # --- Pass 2: collect all needed AnalysisProperty keys ----------
            props_by_key: dict[tuple, AnalysisProperty] = {
                (p.location_id, p.property_type, p.rooms): p
                for p in session.execute(select(AnalysisProperty)).scalars()
            }

            new_prop_rows: list[dict] = []
            seen_new_props: set[tuple] = set()

            for row in silver_rows:
                if not row.city:
                    continue
                loc = locations_by_key.get((row.city, row.neighbourhood))
                if loc is None:
                    continue
                key = (loc.id, row.property_type, row.rooms)
                if key not in props_by_key and key not in seen_new_props:
                    new_prop_rows.append({
                        "location_id": loc.id,
                        "property_type": row.property_type,
                        "rooms": row.rooms,
                    })
                    seen_new_props.add(key)

            # Batch-upsert new AnalysisProperty rows — ON CONFLICT DO NOTHING
            # keeps existing rows untouched, and we count inserts for stats.
            for i in range(0, len(new_prop_rows), UPSERT_BATCH_SIZE):
                chunk = new_prop_rows[i:i + UPSERT_BATCH_SIZE]
                stmt = insert(AnalysisProperty).values(chunk)
                stmt = stmt.on_conflict_do_nothing(
                    constraint="uq_analysis_property_key",
                )
                session.execute(stmt)

            stats["new_groups"] = len(new_prop_rows)

            # Reload all analysis properties so we have ids.
            if new_prop_rows:
                session.flush()
                props_by_key = {
                    (p.location_id, p.property_type, p.rooms): p
                    for p in session.execute(select(AnalysisProperty)).scalars()
                }

            # --- Pass 3: build match rows ----------------------------------
            match_rows: list[dict] = []

            for row in silver_rows:
                stats["processed"] += 1

                if not row.city:
                    stats["skipped_no_location"] += 1
                    continue

                loc = locations_by_key.get((row.city, row.neighbourhood))
                if loc is None:
                    stats["skipped_no_location"] += 1
                    continue

                key = (loc.id, row.property_type, row.rooms)
                prop = props_by_key.get(key)
                if prop is None:
                    # Should not happen after the batch insert, but guard.
                    log.warning(f"No analysis property for key {key}, skipping {row.external_id}")
                    stats["skipped_no_location"] += 1
                    continue

                stats["merged"] += 1
                match_rows.append({
                    "run_id": run_id,
                    "silver_external_id": row.external_id,
                    "analysis_property_id": prop.id,
                    "confidence": 1.0,
                    "method": "new" if key in seen_new_props else "scored_match",
                })

            # Batched upsert — unique (run_id, silver_external_id) means a
            # re-run overwrites its own rows instead of duplicating them.
            for i in range(0, len(match_rows), UPSERT_BATCH_SIZE):
                chunk = match_rows[i:i + UPSERT_BATCH_SIZE]
                stmt = insert(PropertyAnalysisMatch).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["run_id", "silver_external_id"],
                    set_={
                        "analysis_property_id": stmt.excluded.analysis_property_id,
                        "confidence": stmt.excluded.confidence,
                        "method": stmt.excluded.method,
                    },
                )
                session.execute(stmt)

            session.commit()

        tracked.update({
            "processed": stats["processed"],
            "new_analysis_groups": stats["new_groups"],
            "merged_into_existing": stats["merged"],
            "skipped_no_location": stats["skipped_no_location"],
        })

    log.info(f"Analysis matcher: {stats['new_groups']} groups, {stats['merged']} merged, {stats['skipped_no_location']} skipped")
    return stats


if __name__ == "__main__":
    run_id = new_run_id()
    result = run_analysis_matcher(run_id)
    print(f"Analysis matcher complete: {result}")

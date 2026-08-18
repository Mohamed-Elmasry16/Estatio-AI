"""
Bronze layer — takes the raw JSONL files that scraper.py already produces
(unchanged) and lands each hit as-is into Postgres. Append-only.
"""
import json
import glob
from datetime import datetime, timezone

from src.db import SessionLocal, BronzeListing
from sqlalchemy.dialects.postgresql import insert
from src.monitoring import track_stage, new_run_id
from src.logging_config import get_logger

log = get_logger(__name__)


def _load_hits(file: str) -> list[dict]:
    hits = []
    with open(file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                hits.append(json.loads(line))
    return hits


def ingest_bronze(raw_dir: str = "raw_data", run_id: str | None = None) -> dict:
    run_id = run_id or new_run_id()
    stats = {"files": 0, "rows": 0, "skipped_duplicates": 0, "processed_files": []}

    with track_stage(run_id, "bronze") as tracked:
        files = sorted(glob.glob(f"{raw_dir}/*.jsonl"))
        if not files:
            raise FileNotFoundError(f"No raw .jsonl files found in {raw_dir}/ — run scraper.py first.")

        with SessionLocal() as session:
            for file in files:
                hits = _load_hits(file)
                file_inserted = 0
                for hit in hits:
                    scraped_at_raw = hit.get("_scraped_at")
                    scraped_at = (
                        datetime.strptime(scraped_at_raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                        if scraped_at_raw else datetime.now(timezone.utc)
                    )
                    # ON CONFLICT DO NOTHING makes re-runs idempotent — if
                    # the same (external_id, scraped_at) pair already exists
                    # the row is silently skipped instead of creating a dupe.
                    stmt = insert(BronzeListing).values(
                        source="portal_eg",
                        external_id=str(hit.get("externalID") or hit.get("id")),
                        raw_data=hit,
                        scraped_at=scraped_at,
                        run_id=run_id,
                    ).on_conflict_do_nothing(constraint="uq_bronze_external_scraped")
                    result = session.execute(stmt)
                    if result.rowcount > 0:
                        file_inserted += 1
                    else:
                        stats["skipped_duplicates"] += 1
                log.info(f"{file}: ingested {file_inserted} raw hits into bronze_listings ({len(hits) - file_inserted} duplicates skipped)")
                stats["files"] += 1
                stats["rows"] += file_inserted
                stats["processed_files"].append(file)
            session.commit()

        # tracked stats go to the pipeline_runs table — keep that row small,
        # the file list is only needed by the caller to archive afterward.
        tracked.update({"files": stats["files"], "rows": stats["rows"], "skipped_duplicates": stats["skipped_duplicates"]})
        log.info(f"Bronze ingestion done — {{'files': {stats['files']}, 'rows': {stats['rows']}, 'skipped_duplicates': {stats['skipped_duplicates']}}}")
    return stats


if __name__ == "__main__":
    ingest_bronze()
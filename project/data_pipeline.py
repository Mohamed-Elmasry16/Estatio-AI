"""
Runs the full data pipeline: scrape -> bronze -> silver -> gold -> feature_store
-> analysis_matcher. Each stage is a single transaction; any failure stops the
pipeline (exit code 1) so a broken run can't silently produce partial output.

    python data_pipeline.py
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scraper import scrape_all
from src.bronze.ingest import ingest_bronze
from src.silver.transform import transform_silver
from src.gold.build import build_gold
from src.feature_store.build import build_features
from src.gold.analysis_matcher import run_analysis_matcher
from src.monitoring import new_run_id
from src.logging_config import get_logger

log = get_logger(__name__)

RAW_DIR = "raw_data"
ARCHIVE_DIR = "raw_data/_ingested"


def run():
    run_id = new_run_id()
    log.info(f"=== Pipeline run {run_id} ===")

    # Stage 1/6: Scrape
    try:
        log.info("=== Stage 1/6: Scrape ===")
        scrape_all(out_dir=RAW_DIR)
    except Exception as e:
        log.error(f"Scrape failed, stopping pipeline: {e}")
        sys.exit(1)

    # Stage 2/6: Bronze (raw JSONL -> append-only raw table)
    try:
        log.info("=== Stage 2/6: Bronze (raw -> DB) ===")
        ingested = ingest_bronze(raw_dir=RAW_DIR, run_id=run_id)
        Path(ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
        for file in ingested["processed_files"]:
            shutil.move(file, ARCHIVE_DIR)
    except Exception as e:
        log.error(f"Bronze ingestion failed, stopping pipeline: {e}")
        sys.exit(1)

    # Stage 3/6: Silver (clean + validate + dedupe ads)
    try:
        log.info("=== Stage 3/6: Silver (clean + validate + dedupe ads) ===")
        transform_silver(run_id=run_id)
    except Exception as e:
        log.error(f"Silver transform failed, stopping pipeline: {e}")
        sys.exit(1)

    # Stage 4/6: Gold (resolve properties + aggregates + stale marking)
    try:
        log.info("=== Stage 4/6: Gold (resolve properties + aggregates) ===")
        build_gold()
    except Exception as e:
        log.error(f"Gold build failed, stopping pipeline: {e}")
        sys.exit(1)

    # Stage 5/6: Feature Store
    try:
        log.info("=== Stage 5/6: Feature Store ===")
        build_features()
    except Exception as e:
        log.error(f"Feature store build failed: {e}")
        sys.exit(1)

    # Stage 6/6: Analysis matcher (dashboard aggregates)
    try:
        log.info("=== Stage 6/6: Analysis matcher (dashboard aggregates) ===")
        analysis_stats = run_analysis_matcher(run_id=run_id)
        log.info(f"Analysis matcher complete: {analysis_stats}")
    except Exception as e:
        log.error(f"Analysis matcher failed: {e}")
        sys.exit(1)

    log.info("Pipeline complete.")


if __name__ == "__main__":
    run()

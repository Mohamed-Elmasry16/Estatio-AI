"""
Every stage reports into pipeline_runs: how many records in, how many out,
how many rejected/duplicates, how long it took. Answers exactly the
questions you asked for: rejected count, why, duplicate count, stage
duration, records per layer.
"""
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from src.db import SessionLocal, PipelineRun
from src.logging_config import get_logger

log = get_logger(__name__)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def track_stage(run_id: str, stage: str):
    """
    Usage:
        stats = {}
        with track_stage(run_id, "silver") as stats:
            stats["processed"] = 100
            stats["dropped"] = 5
            stats["dropped_reasons"] = {"missing_price": 3, "missing_area": 2}
    Records start/end/duration/status/stats automatically, even on failure.
    """
    started_at = datetime.now(timezone.utc)
    stats: dict = {}
    error = None
    try:
        yield stats
    except Exception as e:
        error = str(e)
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        duration_s = (finished_at - started_at).total_seconds()
        stats["duration_seconds"] = round(duration_s, 2)
        try:
            with SessionLocal() as session:
                session.add(PipelineRun(
                    run_id=run_id,
                    stage=stage,
                    started_at=started_at,
                    finished_at=finished_at,
                    status="failed" if error else "success",
                    stats=stats,
                    error=error,
                ))
                session.commit()
        except Exception as e:
            # Never let a monitoring write failure mask the stage's own
            # error — the run row is best-effort, log and move on.
            log.error(f"Failed to record pipeline_run for stage {stage}: {e}")
        level = logging.ERROR if error else logging.INFO
        log.log(level, f"[{run_id}] {stage} — {stats} {'ERROR: ' + error if error else ''}")


def layer_counts() -> dict:
    """Row counts per layer — 'how many records in each layer'."""
    from sqlalchemy import select, func
    from src.db import BronzeListing, SilverListing, Property, Listing

    with SessionLocal() as session:
        return {
            "bronze_listings": session.scalar(select(func.count()).select_from(BronzeListing)),
            "silver_listings": session.scalar(select(func.count()).select_from(SilverListing)),
            "properties": session.scalar(select(func.count()).select_from(Property)),
            "listings": session.scalar(select(func.count()).select_from(Listing)),
        }

"""
Sanity-check both database connections before running the pipeline for real.
Useful right after setting the real password in .env, and as a container
healthcheck / CI smoke test.

    python scripts/check_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from src.db import engine, migration_engine
from src.logging_config import get_logger

log = get_logger(__name__)


def check(name: str, sa_engine) -> bool:
    try:
        with sa_engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
        log.info(f"{name}: OK — {version.split(',')[0]}")
        return True
    except Exception as e:
        log.error(f"{name}: FAILED — {e}")
        return False


def main() -> int:
    ok_runtime = check("runtime connection (transaction pooler, :6543)", engine)
    ok_migrate = check("migration connection (session/direct, :5432)", migration_engine)
    if ok_runtime and ok_migrate:
        log.info("Both connections OK.")
        return 0
    log.error("One or more connections failed — check DATABASE_URL / DATABASE_MIGRATION_URL in .env.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

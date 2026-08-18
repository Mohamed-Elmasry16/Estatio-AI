"""
Retry policy for transient database errors — the kind you get from a pooled,
network-hop-away Postgres like Supabase's (pooler restarts, brief network
blips, connection recycling) rather than from anything actually wrong with
the query or data. Only used to wrap whole pipeline stages, each of which
commits in a single transaction, so a retry re-runs cleanly from scratch
rather than risking partial application.

Usage:
    from src.retry import with_db_retry

    @with_db_retry
    def ingest_bronze(...):
        ...
"""
from functools import wraps
import logging

from sqlalchemy.exc import DBAPIError, OperationalError
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt,
    wait_exponential, before_sleep_log,
)

from src.config import get_settings
from src.logging_config import get_logger

log = get_logger(__name__)


def with_db_retry(func):
    settings = get_settings()

    policy = retry(
        reraise=True,
        stop=stop_after_attempt(settings.db_retry_attempts),
        wait=wait_exponential(
            multiplier=settings.db_retry_min_wait_seconds,
            max=settings.db_retry_max_wait_seconds,
        ),
        retry=retry_if_exception_type((OperationalError, DBAPIError)),
        before_sleep=before_sleep_log(log, logging.WARNING),
    )

    @wraps(func)
    def wrapper(*args, **kwargs):
        return policy(func)(*args, **kwargs)

    return wrapper
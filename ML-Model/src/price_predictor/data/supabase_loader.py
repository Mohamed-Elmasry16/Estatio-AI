"""
Pulls the raw listings snapshot from Supabase into a pandas DataFrame with
the same shape as the original `egypt_listings_raw.csv`.

Usage today (manual):
    from price_predictor.data.supabase_loader import load_raw_listings
    df = load_raw_listings()

Usage in the future (every 2 weeks, from a scheduler):
    scripts/retrain.py already calls this — just point a cron job / Supabase
    Edge Function schedule / GitHub Actions cron at `python scripts/retrain.py`.
    No code changes needed here when you flip the trigger from manual to
    scheduled; that's the whole point of isolating this in one place.
"""
from __future__ import annotations

import logging

import pandas as pd
from supabase import Client, create_client

from price_predictor import config

logger = logging.getLogger(__name__)


class SupabaseConfigError(RuntimeError):
    """Raised when Supabase credentials are missing or invalid."""


def get_client() -> Client:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        raise SupabaseConfigError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (env vars or .env). "
            "Use the service role key here — this runs server-side only, never ship it to the frontend."
        )
    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)


def load_raw_listings(
    table: str | None = None,
    columns: list[str] | None = None,
    page_size: int | None = None,
) -> pd.DataFrame:
    """
    Fetch the full listings snapshot from Supabase, paging through results
    (PostgREST caps rows per request, so a 44k-row table needs multiple pages).

    Returns a DataFrame with the same columns/dtypes shape as
    egypt_listings_raw.csv, ready to hand to `clean_data()`.
    """
    table = table or config.SUPABASE_LISTINGS_TABLE
    columns = columns or config.RAW_COLUMNS
    page_size = page_size or config.SUPABASE_PAGE_SIZE

    client = get_client()
    select_cols = ",".join(columns)

    all_rows: list[dict] = []
    start = 0
    while True:
        end = start + page_size - 1
        resp = (
            client.table(table)
            .select(select_cols)
            .range(start, end)
            .execute()
        )
        batch = resp.data or []
        all_rows.extend(batch)
        logger.info("Fetched rows %d-%d from %s (%d in this page)", start, end, table, len(batch))

        if len(batch) < page_size:
            break
        start += page_size

    if not all_rows:
        raise ValueError(
            f"Supabase query against table '{table}' returned 0 rows. "
            "Check SUPABASE_LISTINGS_TABLE and that the service key has read access."
        )

    df = pd.DataFrame(all_rows)

    missing_cols = [c for c in columns if c not in df.columns]
    if missing_cols:
        raise ValueError(
            f"Supabase table '{table}' is missing expected columns: {missing_cols}. "
            f"Got columns: {list(df.columns)}"
        )

    logger.info("Loaded %d rows x %d columns from Supabase table '%s'", len(df), len(df.columns), table)
    return df[columns]


def load_raw_listings_from_csv(csv_path: str) -> pd.DataFrame:
    """Fallback for local/manual runs before Supabase wiring is live, or for
    reproducing a past training run from an exported snapshot."""
    df = pd.read_csv(csv_path)
    missing_cols = [c for c in config.RAW_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"CSV at {csv_path} is missing expected columns: {missing_cols}")
    return df[config.RAW_COLUMNS]

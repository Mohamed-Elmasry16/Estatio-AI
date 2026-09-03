#!/usr/bin/env python3
"""
Retrain entrypoint.

Manual use today:
    python scripts/retrain.py                     # pulls fresh data from Supabase
    python scripts/retrain.py --csv path/to.csv    # or use a local CSV export

Scheduled use later (every 2 weeks): point a cron job / Supabase scheduled
Edge Function / GitHub Actions cron / systemd timer at this exact command —
no code changes needed:
    0 3 */14 * *  cd /path/to/project && python scripts/retrain.py --reload-api

If --reload-api is passed (or API_RELOAD_URL is set in the environment),
the script hits POST /admin/reload on the running API after a successful
retrain so it starts serving the new model immediately, no restart needed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from price_predictor import config  # noqa: E402
from price_predictor.data.supabase_loader import load_raw_listings, load_raw_listings_from_csv  # noqa: E402
from price_predictor.pipeline.train import retrain  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=str, default=None, help="Use a local CSV instead of pulling from Supabase.")
    parser.add_argument("--model", type=str, default=None, help="Force a specific model name instead of re-running CV comparison.")
    parser.add_argument("--skip-holdout-eval", action="store_true", help="Skip the train/test holdout split (faster, but no reported metrics).")
    parser.add_argument("--reload-api", action="store_true", help="POST to the running API's /admin/reload after a successful retrain.")
    parser.add_argument("--api-url", type=str, default=os.getenv("API_RELOAD_URL", "http://localhost:8000"), help="Base URL of the running API, used with --reload-api.")
    args = parser.parse_args()

    logger.info("Loading raw listings...")
    if args.csv:
        raw_df = load_raw_listings_from_csv(args.csv)
    else:
        raw_df = load_raw_listings()
    logger.info("Loaded %d raw rows.", len(raw_df))

    artifact = retrain(
        raw_df,
        model_name=args.model,
        do_holdout_eval=not args.skip_holdout_eval,
    )

    logger.info(
        "Retrain complete: model=%s rows=%d holdout=%s",
        artifact["model_name"], artifact["n_training_rows"], artifact["holdout_metrics"],
    )

    if args.reload_api:
        url = f"{args.api_url.rstrip('/')}/admin/reload"
        headers = {"X-API-Key": config.API_KEY} if config.API_KEY else {}
        try:
            resp = requests.post(url, headers=headers, timeout=15)
            resp.raise_for_status()
            logger.info("API reloaded successfully: %s", resp.json())
        except requests.RequestException as e:
            logger.error("Retrain succeeded but reloading the API failed: %s", e)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

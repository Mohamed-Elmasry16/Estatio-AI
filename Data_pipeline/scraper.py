"""
Egypt property listings scraper — queries the listing platform's search
index directly. No HTML parsing, no browser automation. Just paginated
JSON requests.

All source-specific settings (app id, api key, index name, endpoint
template) come from the environment via src/config.py — see .env.example.
"""
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.config import get_settings
from src.logging_config import get_logger
from src.silver.transform import build_listing_url

log = get_logger(__name__)

settings = get_settings()

_REQUIRED_SCRAPER_ENV = {
    "SCRAPER_APP_ID": settings.scraper_app_id,
    "SCRAPER_API_KEY": settings.scraper_api_key,
    "SCRAPER_INDEX_NAME": settings.scraper_index_name,
    "SCRAPER_BASE_URL_TEMPLATE": settings.scraper_base_url_template,
}
_missing = [name for name, value in _REQUIRED_SCRAPER_ENV.items() if not value]
if _missing:
    raise RuntimeError(
        "Missing scraper configuration in .env: " + ", ".join(_missing)
    )

APP_ID = settings.scraper_app_id
API_KEY = settings.scraper_api_key
INDEX_NAME = settings.scraper_index_name
BASE_URL = settings.scraper_base_url_template.replace("{app_id}", APP_ID)
AGENT = settings.scraper_agent

HITS_PER_PAGE = 25          # safe, conservative page size
REQUEST_DELAY_SEC = 1.0     # be polite, avoid getting rate-limited/blocked
MAX_RETRIES = 3

# Only ask for what we actually need — smaller payloads, faster runs
ATTRIBUTES_TO_RETRIEVE = [
    "id", "externalID", "title", "title_l1",
    "purpose", "price", "downPayment", "rentFrequency",
    "rooms", "baths", "area", "plotArea",
    "location", "category", "geography",
    "furnishingStatus", "completionStatus",
    "createdAt", "updatedAt", "deal",
    "agency", "photoCount",
]

# purpose:for-sale, residential only, for now — extend later (rentals, commercial)
FILTERS = 'purpose:"for-sale" AND (category.slug:"residential")'


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _build_params(page: int) -> str:
    """Build the search index query params string, replicating what the site itself sends."""
    params = {
        "page": page,
        "hitsPerPage": HITS_PER_PAGE,
        "query": "",
        "filters": FILTERS,
        "attributesToRetrieve": json.dumps(ATTRIBUTES_TO_RETRIEVE),
    }
    # The index API expects a URL-encoded query string, not JSON, for the "params" field
    from urllib.parse import urlencode
    return urlencode(params)


def _fetch_page(page: int) -> dict:
    headers = {
        "x-algolia-agent": AGENT,
        "x-algolia-api-key": API_KEY,
        "x-algolia-application-id": APP_ID,
        "Content-Type": "application/json",
    }
    body = {
        "requests": [{
            "indexName": INDEX_NAME,
            "params": _build_params(page),
        }]
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(BASE_URL, headers=headers, json=body, timeout=15)
            resp.raise_for_status()
            return resp.json()["results"][0]
        except (requests.RequestException, KeyError, IndexError) as e:
            log.warning(f"Page {page} attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2 * attempt)  # backoff


def scrape_all(out_dir: str = "raw_data") -> str:
    """Fetch every page and write raw hits to a timestamped JSONL file. Returns the file path."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = out_path / f"listings_raw_{run_ts}.jsonl"

    page = 0
    total_hits = 0
    nb_pages = None

    with open(out_file, "w", encoding="utf-8") as f:
        while nb_pages is None or page < nb_pages:
            result = _fetch_page(page)
            hits = result["hits"]
            nb_pages = result["nbPages"]

            if not hits:
                log.warning(f"Page {page} returned 0 hits before expected end (nbPages={nb_pages}) — stopping.")
                break

            for hit in hits:
                hit["_scraped_at"] = run_ts
                hit["url"] = build_listing_url(
                    str(hit.get("externalID") or hit.get("id")),
                    settings.listing_url_template,
                )
                f.write(json.dumps(hit, ensure_ascii=False) + "\n")

            total_hits += len(hits)
            log.info(f"Page {page + 1}/{nb_pages} — {len(hits)} hits (total so far: {total_hits})")

            page += 1
            time.sleep(REQUEST_DELAY_SEC)

    log.info(f"Done. {total_hits} listings written to {out_file}")

    if total_hits == 0:
        # This is the "scraper broke" signal from the plan — surface it loudly
        raise RuntimeError("Scrape returned 0 listings. Site structure or filters may have changed.")

    return str(out_file)


if __name__ == "__main__":
    scrape_all()
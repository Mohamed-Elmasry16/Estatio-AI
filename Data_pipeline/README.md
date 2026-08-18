# Estatio-AI — Real Estate Data Pipeline

Scrapes Egyptian property listings, dedupes them into canonical apartments, builds ML features + vector embeddings. Postgres (Supabase) + pgvector.

```
Scraper (Algolia) → Bronze (raw) → Silver (clean) → Gold (entity resolution)
                                                        ├→ Feature Store
                                                        ├→ Analysis Matcher (dashboards)
                                                        └→ Vectors (opt-in, needs NVIDIA_API_KEY)
```

## Quick start

```bash
cp .env.example .env              # fill in DATABASE_URL + DATABASE_MIGRATION_URL (Supabase password)
pip install -r requirements.txt
python scripts/check_db.py        # verify both DB connections
alembic upgrade head              # create schema + pgvector extension

python data_pipeline.py           # run all 6 stages
python build_vectors.py           # optional
```

Or `make install / check-db / migrate / pipeline / vectors / test`. Or Docker: `docker compose run --rm migrate && docker compose run --rm pipeline`.

Local Postgres instead of Supabase: `docker compose -f docker-compose.dev.yml up -d`.

**Never commit `.env`.** If a password leaks, rotate it in Supabase → Database → Reset password.

## The core problem this solves

Same apartment gets posted in 4+ ads, with no street address and GPS pins that are often just the **compound centroid**, shared by hundreds of units. Naive matching on rooms/area/coordinates merges unrelated apartments together.

**Fix — `src/gold/matcher.py`, weighted scoring instead of greedy first-match:**
- Parses the Arabic title for phase (`B12`), unit type (roof/garden/duplex), view (sea/lagoon) — these disambiguate compound units far better than GPS.
- **Hard vetoes** (instant reject, no scoring): phase mismatch, building mismatch, incompatible unit type, area diff >30%, distance >500m, same GPS pin with no title evidence.
- **Weighted score** across type/rooms/baths/area/geo/price/title (min confidence 0.65–0.80 to merge). GPS is only 10% of the score — deliberately downweighted since it's often just a marketing pin.
- Price tolerance scales by tier: 10% luxury (≥10M EGP), 15% mid, 20% budget.
- **Always prefers a false negative** (new Property) over a false positive (wrong merge) — wrong merges corrupt price history.
- Every Property stores `match_method` + `match_confidence` for auditing.
- 20+ tests in `tests/test_matcher.py`.

There are **two matchers**: `matcher.py` (high precision, feeds `properties`/`listings`) and `analysis_matcher.py` (high recall, exact `(location, type, rooms)` key, feeds dashboard tables — no title/distance/price logic).

## Two database connections (Supabase-specific)

| | Port | Used for |
|---|---|---|
| `DATABASE_URL` | 6543 (transaction pooler) | all runtime reads/writes |
| `DATABASE_MIGRATION_URL` | 5432 (direct) | `alembic upgrade`, `CREATE EXTENSION` |

Why: PgBouncer's transaction pooling breaks DDL and server-side prepared statements. Setting only `DATABASE_URL` auto-derives the migration URL (swap `:6543`→`:5432`), but setting both explicitly is safer.

## Structure

```
scraper.py, data_pipeline.py, build_vectors.py
src/
  config.py, db.py, logging_config.py, retry.py, monitoring.py
  bronze/ingest.py        raw JSONL → bronze_listings
  silver/transform.py     clean/validate/dedupe → silver_listings
  gold/matcher.py          scoring engine
  gold/build.py            → properties, listings, price_history
  gold/analysis_matcher.py → analysis_properties
  feature_store/build.py   → property_features
  vector/                  document_builder.py, embedder.py, store.py → property_vectors
alembic/            schema migrations (0001 initial, 0002 bronze idempotency)
scripts/            check_db.py, diagnose_grouping.py, print_config.py
tests/               pytest, no live DB needed
```

## Database (12 tables)

Bronze: `bronze_listings` (append-only, idempotent). Silver: `silver_listings` (upsert by `external_id`). Gold: `locations`, `buildings` (placeholder, unused), `properties`, `listings`, `price_history`. Analytics: `analysis_properties`, `property_analysis_matches`. `property_features` (feature store), `property_vectors` (2048-dim embeddings, exact cosine scan — pgvector's ANN index caps at 2000 dims). `pipeline_runs` (monitoring).

## Key env vars

| Var | Default | Notes |
|---|---|---|
| `DATABASE_URL` / `DATABASE_MIGRATION_URL` | — | see above |
| `DB_STATEMENT_TIMEOUT_MS` | 60000 | |
| `DB_RETRY_ATTEMPTS` | 3 | exponential backoff, 1s–8s |
| `LOG_FORMAT` | `text` | `json` for prod log aggregators |
| `SCRAPER_APP_ID/API_KEY/INDEX_NAME` | — | Algolia creds |
| `NVIDIA_API_KEY` | — | required for `build_vectors.py` |
| `EMBEDDING_MODEL` | `nvidia/nemotron-3-embed-1b` | 2048-dim |

Placeholder passwords (`[YOUR-PASSWORD]`) fail fast at startup. Full var list in-code (`src/config.py`).

## Resilience

Each DB stage retries transient errors (3x, exponential backoff) and commits as one transaction. Connections use `pool_pre_ping` (dead connections replaced automatically) and a statement timeout so stuck queries can't hold a pooled connection forever.

## Schema changes → Alembic, never `init_db()`

`init_db()` is dev-only (no migration history). For real changes:
```bash
alembic revision --autogenerate -m "add whatever_column"
# review the file, then:
alembic upgrade head
```

## Tests

```bash
pytest tests/ -v   # or: make test
ruff check src scripts tests *.py
```
All pure logic, no live DB — CI runs both on every push with zero secrets.

## Known limitations

- `Building` table unused until there's real building-level source data.
- Matching thresholds (area %, distance, price tolerance) are a first pass — tune once you see real duplicate rates.
- Stale listings don't auto-reactivate if they reappear in a later scrape.
- No ANN index on embeddings — fine at current scale (~16–50k rows), revisit past ~100k.
- No DB-integration tests yet (only pure-logic tests).
- Confirm the source portal's ToS allows commercial use of this data.
- Consider Row Level Security if `property_vectors` is ever exposed to an external client.

## Roadmap

**Near-term**: monitoring dashboard + alerts on over-large Properties, manual review queue, backfill script for existing Properties under the new matcher.
**Later**: embedding-based title similarity, graph-based clustering, cross-source matching, temporal re-listing detection.

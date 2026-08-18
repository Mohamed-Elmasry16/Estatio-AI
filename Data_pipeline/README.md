# Real Estate Data Pipeline — Scrape → Bronze → Silver → Gold → Feature Store (→ Vectors)

Production setup, backed by **Supabase Postgres** (with pgvector).

## Structure
```
scraper.py                  ← hits the listing platform's search index
data_pipeline.py            ← runs all 6 stages in order, stops on failure
build_vectors.py            ← separate script, stage 7 (needs NVIDIA_API_KEY)

src/
├── config.py                ← all settings from env vars (pydantic-settings)
├── db.py                     ← SQLAlchemy models + two engines (see "Two DB
│                               connections" below)
├── logging_config.py         ← central logging (text locally, JSON in prod)
├── retry.py                   ← retry/backoff wrapper for transient DB errors
├── monitoring.py              ← track_stage() wraps every stage: counts,
│                                duration, errors -> pipeline_runs table
├── bronze/ingest.py           ← raw JSONL -> bronze_listings (append-only)
├── silver/transform.py        ← flatten + validate + dedupe ADS -> silver_listings
├── gold/
│   ├── matcher.py              ← the property-matching heuristic (pure, unit-tested)
│   ├── build.py                ← resolves ads to canonical Properties,
│   │                             tracks PriceHistory + stale status, computes
│   │                             price_per_m2/outliers from ACTIVE ads only
│   └── analysis_matcher.py     ← stable dashboard groupings (analysis_properties)
├── feature_store/build.py      ← one row per Property with training/serving-ready
│                                  features -> property_features table
└── vector/
    ├── document_builder.py      ← bilingual Property features -> natural-language text
    ├── embedder.py               ← embedding API client (timeout + retry)
    └── store.py                   ← runs the above, writes property_vectors (HNSW)

alembic/                      ← versioned schema migrations (see below)
scripts/check_db.py            ← connectivity smoke test for both DB connections
scripts/diagnose_grouping.py   ← diagnostic for the analysis grouping key
tests/                          ← pytest, no live DB required
.github/workflows/ci.yml        ← runs pytest on every push/PR
Dockerfile, docker-compose.yml  ← containerized runs
```

## The canonical model (fixes "same apartment in 4 ads")

```
Location  — normalized city/neighbourhood
Building  — placeholder, mostly unused until the source exposes building-level data
Property  — the canonical PHYSICAL unit (one real apartment, our best guess)
Listing   — one AD, points at a Property, has its own price/status
PriceHistory — append-only price observations per listing
```

Listings are marked `stale` when an ad is absent from the latest scrape
(and reactivated if it comes back). Aggregates — `current_price_egp`,
`price_per_m2`, `active_listing_count` — only consider active ads, so a
delisted ad stops dragging a property's numbers down.

**Honest limitation**: the source data has no street address, only lat/lng +
neighbourhood + rooms/area. `src/gold/matcher.py` matches on
(neighbourhood + property_type + rooms + area within 5% + price within 50% +
location within ~50m, hard-capped at 1km). It's deliberately conservative —
prefers creating a new Property over wrongly merging two different
apartments. Title signals (building code / phase / floor type / view) are
compared against the union of ALL of a property's listings, so a new ad
that disagrees with any existing ad is hard-rejected. Every Property
records how it was created or last matched (`new` / `scored_match` /
`split`) plus a confidence score, so you can audit false
positives/negatives later and tighten the rules. `tests/test_matcher.py`
covers the core cases.

## Database: Supabase, and why there are two connection strings

Supabase puts **PgBouncer** in front of Postgres and gives you two ports:

| | Port | Mode | Used for |
|---|---|---|---|
| `DATABASE_URL` | `6543` | Transaction pooler | All runtime pipeline reads/writes |
| `DATABASE_MIGRATION_URL` | `5432` | Session pooler / direct | `alembic upgrade`, `CREATE EXTENSION vector` |

The transaction pooler is great for a batch pipeline (lots of short
connections) but it multiplexes the underlying Postgres connection between
statements, so it **doesn't reliably support DDL or server-side prepared
statements**. `src/db.py` handles this two ways:

1. Two SQLAlchemy engines: `engine` (pooled, `NullPool` — PgBouncer is
   already pooling, so a second pool on top just adds confusion) for
   runtime queries, and `migration_engine` (direct/session, small
   persistent pool) for anything that needs DDL.
2. `prepare_threshold=None` on the pooled connection — disables psycopg3's
   automatic server-side prepared statements, which is the #1 cause of
   `prepared statement "..." already exists / does not exist` errors
   against transaction-mode PgBouncer.

If you only set `DATABASE_URL`, `DATABASE_MIGRATION_URL` is derived
automatically by swapping `:6543` for `:5432` — one env var is enough to
get started, but setting both explicitly is more robust (Supabase's
session-pooler host is sometimes not just "same host, different port").

## Setup

```bash
cp .env.example .env
```

Edit `.env` and replace `[YOUR-PASSWORD]` (in **both** `DATABASE_URL` and
`DATABASE_MIGRATION_URL`) with your actual Supabase database password —
find it in the Supabase dashboard under **Project Settings → Database →
Connection string** (or reset it there if you don't have it: **Database →
Reset password**).

> **Never commit `.env`** — it's already in `.gitignore`. If a real
> password is ever pasted somewhere outside your local machine (a chat, a
> ticket, a public repo), rotate it immediately from that same dashboard
> page.

```bash
pip install -r requirements.txt

# Verify both connections work before doing anything else
python scripts/check_db.py

# Apply the schema (creates all tables + the pgvector extension)
alembic upgrade head
```

`make install`, `make check-db`, `make migrate` do the same three
commands, if you'd rather use the Makefile.

### Local Postgres instead of Supabase (optional)

If you want to develop without touching Supabase at all:

```bash
docker compose -f docker-compose.dev.yml up -d
# then point DATABASE_URL at localhost:5432 in .env and run alembic upgrade head
```

## Run

```bash
python data_pipeline.py        # scrape -> bronze -> silver -> gold -> feature_store -> analysis_matcher
python build_vectors.py        # optional, only once NVIDIA_API_KEY is set
```

Or via Docker:

```bash
docker compose run --rm migrate    # alembic upgrade head
docker compose run --rm pipeline   # the 5-stage pipeline
docker compose --profile vectors run --rm build-vectors
```

## Schema changes go through Alembic, not `init_db()`

`src.db.init_db()` still exists as a dev convenience (creates tables
straight from the models, no history) — fine for a throwaway local
Postgres, **not** for Supabase/staging/production. For anything real:

```bash
# after changing a model in src/db.py, src/feature_store/build.py, or src/vector/store.py:
alembic revision --autogenerate -m "add whatever_column"
# review the generated file in alembic/versions/ before applying it
alembic upgrade head
```

`alembic/env.py` always targets `migration_engine` (the direct/session
connection), never the pooled runtime connection.

## Monitoring

Every stage writes to `pipeline_runs`: start/end time, duration, and a
JSON `stats` blob — records processed, dropped (with reason breakdown),
duplicates collapsed, new vs. matched properties. Query it directly:

```sql
SELECT stage, status, stats, finished_at - started_at AS duration
FROM pipeline_runs ORDER BY started_at DESC LIMIT 20;
```

`src.monitoring.layer_counts()` gives row counts per layer — printed
automatically at the end of `data_pipeline.py`.

## Resilience

- **Retries**: each pipeline stage is wrapped with exponential-backoff
  retry (`src/retry.py`, via `tenacity`) for transient `OperationalError`/
  `DBAPIError` — the kind you get from a pooler restart or brief network
  blip, not from bad data. Each stage commits as a single transaction, so
  a retry re-runs cleanly rather than risking partial writes.
- **Statement timeout**: every connection sets `statement_timeout` (default
  60s, `DB_STATEMENT_TIMEOUT_MS`) so a stuck query can't hold a pooled
  connection forever.
- **`pool_pre_ping`**: on for both engines — a dead connection is detected
  and replaced before it's handed to your code, instead of failing mid-query.

## Logging

Structured logging via `src/logging_config.py`:
- `LOG_FORMAT=text` (default) — human-readable, for local dev.
- `LOG_FORMAT=json` — single-line JSON per log entry, for shipping to a log
  aggregator in staging/production. Set `ENVIRONMENT=production` and
  `LOG_FORMAT=json` in `.env` for a deployed run.

## Tests

```bash
pytest tests/ -v          # or: make test
ruff check src scripts tests *.py   # or: make lint
```

31 tests, all pure logic (matcher, split pass, flatten/validation, document
builder) — no live database needed, so CI (`.github/workflows/ci.yml`) runs
them (plus ruff) on every push with no secrets configured.

## Still open

- `Building` stays empty until there's a real source for building/compound
  identity — don't rely on it yet.
- The matching heuristic is a first pass — expect to tune the 5%/50m/1km
  thresholds once you see real duplicate/false-match rates.
- DB-integration tests (actually hitting Postgres) are a reasonable next
  addition — e.g. spin up `docker-compose.dev.yml` in CI and run a smaller
  set of tests against it.
- Confirm the source platform's ToS allow this data to be used commercially.
- Consider Supabase Row Level Security if this database is ever exposed to
  anything beyond this pipeline (e.g. a future chatbot reading
  `property_vectors` directly with an anon key).

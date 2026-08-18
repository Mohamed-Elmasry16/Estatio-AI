# Estatio-AI — Real Estate Data Pipeline

**Scrape → Bronze → Silver → Gold (Entity Resolution) → Feature Store → Analysis Matcher → (Vectors)**

A production-grade ETL pipeline for the Egyptian real estate market. It scrapes listings from a
property portal's Algolia search index, cleans and validates them, resolves duplicate ads into
canonical physical properties, computes ML-ready features, and (optionally) generates vector
embeddings for semantic search. Backed by **Supabase Postgres** with **pgvector**.

---

## What this system does

1. **Scrapes** residential for-sale listings directly from the portal's Algolia REST API (no
   headless browser).
2. **Ingests** raw JSON payloads into an append-only Bronze tier.
3. **Cleans & validates** records into a Silver tier with quality gates.
4. **Resolves entities** — matches multiple ads describing the same physical apartment into one
   canonical `Property`, even without unique unit IDs and with GPS pins that are often just
   compound-level marketing coordinates.
5. **Tracks price history** per listing over time.
6. **Computes ML features** (feature store) and, optionally, **generates vector embeddings**
   (pgvector) for semantic search.
7. **Produces dashboard aggregations** via a separate, high-recall analysis matcher.

```
┌─────────┐    ┌─────────┐    ┌──────────┐    ┌────────────────────┐
│ Scraper │───▶│ Bronze  │───▶│  Silver  │───▶│     Gold Layer      │
│(Algolia)│    │(raw JSON│    │(cleaned &│    │ (entity resolution  │
│         │    │ append) │    │validated)│    │ + canonical model)  │
└─────────┘    └─────────┘    └──────────┘    └──────────┬──────────┘
                                                          │
                                    ┌─────────────────────┼──────────────┐
                                    ▼                     ▼              ▼
                             ┌──────────┐          ┌───────────┐   ┌──────────┐
                             │ Feature  │          │ Analysis  │   │  Price   │
                             │  Store   │          │  Matcher  │   │ History  │
                             └────┬─────┘          └───────────┘   └──────────┘
                                  ▼
                            ┌──────────┐
                            │  Vector  │
                            │Embeddings│  (opt-in, separate script)
                            │(pgvector)│
                            └──────────┘
```

---

## Structure

```
scraper.py                   ← hits the portal's Algolia search index
data_pipeline.py              ← runs stages 1–6 in order, halts on failure
build_vectors.py              ← separate script, opt-in stage (needs NVIDIA_API_KEY)

src/
├── config.py                  ← all settings from env vars (pydantic-settings)
├── db.py                       ← SQLAlchemy models (12 tables) + two engines
├── logging_config.py           ← central logging (text locally, JSON in prod)
├── retry.py                     ← @with_db_retry — exponential backoff for transient DB errors
├── monitoring.py                ← track_stage() wraps every stage: counts, duration,
│                                   errors -> pipeline_runs table
├── bronze/ingest.py             ← raw JSONL -> bronze_listings (append-only, idempotent)
├── silver/transform.py          ← flatten + validate + dedupe ads -> silver_listings
├── gold/
│   ├── matcher.py                ← weighted-scoring entity resolution engine (pure, unit-tested)
│   ├── build.py                  ← resolves ads to canonical Properties, tracks PriceHistory
│   │                                and stale status, computes price_per_m2/outliers from
│   │                                ACTIVE ads only
│   ├── analysis_matcher.py       ← stable, high-recall dashboard groupings (analysis_properties)
│   └── audit_duplicates.py       ← CLI dedup audit & CSV export tool
├── feature_store/build.py        ← one row per Property with training/serving-ready
│                                    features -> property_features table
└── vector/
    ├── document_builder.py        ← bilingual Property features -> natural-language text
    ├── embedder.py                 ← NVIDIA NIM embedding API client (timeout + retry)
    └── store.py                    ← runs the above, writes property_vectors, incremental

alembic/                        ← versioned schema migrations
│   ├── env.py                    ← always targets the direct/session (migration) connection
│   └── versions/                 ← 0001 initial schema (12 tables + pgvector), 0002 bronze idempotency
scripts/
├── check_db.py                   ← connectivity smoke test for both DB connections
├── diagnose_grouping.py          ← diagnostic for the analysis grouping key
└── print_config.py               ← env sanity check (BOM detection, redacted URLs, localhost warning)
tests/                            ← pytest, no live DB required
.github/workflows/ci.yml          ← runs pytest + ruff on every push/PR
Dockerfile, docker-compose.yml    ← containerized runs
```

---

## The canonical model (fixes "same apartment in 4 ads")

```
Location  — normalized (city, neighbourhood) pair
Building  — placeholder, mostly unused until the source exposes building-level data
Property  — the canonical PHYSICAL unit (one real apartment, our best guess)
Listing   — one AD, points at a Property, has its own price/status
PriceHistory — append-only price observations per listing
```

Listings are marked `stale` when an ad is absent from the latest scrape. **Note**: there is
currently no mechanism to flip a stale listing back to `active` if it reappears in a later scrape —
see [Known limitations](#known-limitations). Aggregates — `current_price_egp`, `price_per_m2`,
`active_listing_count` — only consider active ads, so a delisted ad stops dragging a property's
numbers down.

### Why this is hard

The source data has no street address — only lat/lng, neighbourhood, and rooms/area. Worse, large
compounds (e.g. Madinaty, Palm Hills) have hundreds of units sharing an identical floor plan *and*
the same marketing GPS pin (the compound centroid, not the unit). A naive "same rooms + same area +
same coordinates → merge" heuristic will happily merge 176 distinct apartments into one Property.

### The matcher: weighted scoring, not greedy first-match

`src/gold/matcher.py` was redesigned around one core principle: **never merge on the first
acceptable candidate — score every candidate and only merge if the best one clears a high bar.**
Preferring a false negative (an extra Property that can be reconciled later) over a false positive
(a wrong merge that corrupts price history) is a deliberate, permanent design choice.

**Hard vetoes** (any one instantly disqualifies a candidate, before scoring):

| Rule | Threshold |
|---|---|
| Phase mismatch | e.g. `B7` ≠ `B11` |
| Building/tower number mismatch | e.g. Tower 1 ≠ Tower 5 |
| Unit-type conflict | Garden ∩ Roof = ∅ |
| Area difference | > 30% |
| Geo distance | > 500m (hard ceiling: 1000m) |
| Compound centroid, no title evidence | Same GPS pin with no phase/building signal → reject |

**Weighted scoring** (candidates that survive vetoes; weights sum to 1.0):

| Feature | Weight | Notes |
|---|---|---|
| Property type | 0.10 | Exact match required |
| Rooms | 0.15 | Exact match required |
| Bathrooms | 0.05 | Optional field |
| Area | 0.20 | Linear decay; tolerance is dynamic — 2% with coordinates for units >200m², more lenient (5%) without |
| Geo | 0.10 | Exponential decay, deliberately downweighted (see below) |
| Price | 0.15 | Dynamic tolerance by price tier (below) |
| Title keywords | 0.15 | Phase / unit-type match, parsed from the Arabic title |
| Title similarity | 0.10 | Jaccard token overlap |

Minimum confidence to accept a match: **0.65** in general, **≥0.80** for the entity matcher's
production threshold. Every `Property` records `match_method` (`new` / `scored_match` / `split`)
and `match_confidence`, so false positives/negatives can be audited and thresholds tuned later.
`tests/test_matcher.py` (20+ cases) covers distance, scoring, hard vetoes, title signals, and
dynamic area tolerance.

**Title signal extraction** (the key innovation) — Arabic-aware regex over the ad title pulls out:
- **Phase**: `B1`, `B12`, `Phase A`, `Village 1`, `Tower 5` — different phases are almost always
  different buildings.
- **Unit type**: roof, garden, corner, duplex, penthouse (Arabic + English) — physically
  incompatible types hard-veto each other.
- **View**: lagoon, golf, sea, park, club — a minor scoring signal, but a hard veto for chalets
  (sea view vs. lagoon view are never the same unit).

**Why GPS is downweighted to 10%**: an audit surfaced 176 listings sharing identical coordinates —
clearly a marketing pin for the whole compound, not per-unit GPS. Title signals (phase, building)
are far more discriminative than coordinates in this dataset; GPS is kept mainly as a tiebreaker
and as a hard ceiling (>1km apart can never be the same unit) rather than as the primary signal.

**Dynamic price tolerance** — fixed 25% flat tolerance was replaced with a price-tiered scale,
since luxury pricing is comparatively stable while budget listings show more negotiation variance:

```python
if price >= 10_000_000:   tolerance = 0.10   # luxury
elif price >= 3_000_000:  tolerance = 0.15   # mid-range
else:                      tolerance = 0.20   # budget
```

**Candidate indexing**: candidates are indexed by `(location_id, property_type, rooms)` rather than
`location_id` alone — roughly a 10x reduction in per-listing candidate scans (500 → ~50), on top of
the existing O(N) approach, without changing asymptotic complexity. There's also an operational cap,
`MAX_LISTINGS_PER_PROPERTY = 8`, on the analysis-side grouping.

**Rule-based, not ML** — deliberately. Every match is 100% explainable and debuggable, there's no
training data or model-drift risk, and it works from day one with no cold start. The architecture
leaves room to swap in ML for individual scorers (e.g. embeddings for title similarity) later
without changing the overall design.

### Entity matcher vs. analysis matcher

Two matchers exist on purpose, tuned for different goals:

| Dimension | Entity matcher (`matcher.py`) | Analysis matcher (`analysis_matcher.py`) |
|---|---|---|
| Objective | High-precision physical unit resolution | High-recall dashboard grouping |
| Grouping key | `(location_id, property_type, rooms)` + multi-signal scoring | `(location_id, property_type, rooms)` exact match |
| Title intelligence | Full parsing (floor, view, phase, building) | None |
| Distance ceiling | 1000m hard limit; <5m centroid enforcement | None |
| Price guard | 50% hard reject + tiered soft scale | None |
| Confidence threshold | ≥ 0.80 | Fixed at 1.0 |
| Output tables | `properties`, `listings`, `price_history` | `analysis_properties`, `property_analysis_matches` |

`AnalysisProperty` IDs are stable across runs (unique constraint on the grouping key), so dashboard
metrics don't jump around between pipeline executions.

---

## Database: Supabase, and why there are two connection strings

Supabase puts **PgBouncer** in front of Postgres and exposes two ports:

| | Port | Mode | Used for |
|---|---|---|---|
| `DATABASE_URL` | `6543` | Transaction pooler | All runtime pipeline reads/writes |
| `DATABASE_MIGRATION_URL` | `5432` | Session pooler / direct | `alembic upgrade`, `CREATE EXTENSION vector` |

The transaction pooler is great for a batch pipeline (lots of short connections) but multiplexes
the underlying Postgres connection between statements, so it **doesn't reliably support DDL or
server-side prepared statements**. `src/db.py` handles this two ways:

1. Two SQLAlchemy engines: `engine` (`NullPool` — PgBouncer is already pooling, so a second pool
   on top just adds confusion) for runtime queries, and `migration_engine` (`QueuePool`,
   direct/session) for anything that needs DDL.
2. `prepare_threshold=None` on the pooled connection — disables psycopg3's automatic server-side
   prepared statements, the #1 cause of `prepared statement "..." already exists / does not exist`
   errors against transaction-mode PgBouncer.

If you only set `DATABASE_URL`, `DATABASE_MIGRATION_URL` is derived automatically by swapping
`:6543` for `:5432` — one env var is enough to get started, but setting both explicitly is more
robust (Supabase's session-pooler host is sometimes not just "same host, different port"). At
startup, `pydantic-settings` rejects placeholder passwords: if either URL still contains
`[YOUR-PASSWORD]`, config raises a `ValueError` immediately rather than failing later mid-pipeline.

---

## Setup

```bash
cp .env.example .env
```

Edit `.env` and replace `[YOUR-PASSWORD]` (in **both** `DATABASE_URL` and
`DATABASE_MIGRATION_URL`) with your actual Supabase database password — find it in the Supabase
dashboard under **Project Settings → Database → Connection string** (or reset it there if you don't
have it: **Database → Reset password**).

> **Never commit `.env`** — it's already in `.gitignore`. If a real password is ever pasted
> somewhere outside your local machine (a chat, a ticket, a public repo), rotate it immediately
> from that same dashboard page.

```bash
pip install -r requirements.txt

# Verify both connections work before doing anything else
python scripts/check_db.py

# Apply the schema (creates all 12 tables + the pgvector extension)
alembic upgrade head
```

`make install`, `make check-db`, `make migrate` do the same three commands if you'd rather use the
Makefile. `python scripts/print_config.py` is a handy sanity check too — it verifies `.env` exists,
detects a stray UTF-8 BOM (a common silent-parsing-failure cause), prints redacted DB URLs, and
warns if `DATABASE_URL` still points at localhost.

### Local Postgres instead of Supabase (optional)

```bash
docker compose -f docker-compose.dev.yml up -d
# then point DATABASE_URL at localhost:5432 in .env and run alembic upgrade head
```

This spins up `pgvector/pgvector:pg16` with `postgres:postgres` credentials and a `real_estate`
database on port 5432, with a persistent volume.

---

## Run

```bash
python data_pipeline.py        # scrape -> bronze -> silver -> gold -> feature_store -> analysis_matcher
python build_vectors.py        # optional, only once NVIDIA_API_KEY is set
```

Or via Docker:

```bash
docker compose run --rm migrate    # alembic upgrade head
docker compose run --rm pipeline   # the 6-stage pipeline (pipeline service depends on migrate)
docker compose --profile vectors run --rm build-vectors
```

The pipeline is strictly sequential — 6 stages, each wrapped in `try/except`; if any stage fails,
the process exits immediately (`sys.exit(1)`) rather than risk partial downstream writes:

| Stage | Function | Output |
|---|---|---|
| 1/6 | `scrape_all()` | JSONL file in `raw_data/` |
| 2/6 | `ingest_bronze()` | `bronze_listings`; processed files archived to `raw_data/_ingested/` |
| 3/6 | `transform_silver()` | `silver_listings` (upsert) |
| 4/6 | `build_gold()` | `properties`, `listings`, `price_history`; stale ads marked |
| 5/6 | `build_features()` | `property_features` |
| 6/6 | `run_analysis_matcher()` | `analysis_properties`, `property_analysis_matches` |

Every run gets a unique 12-character `run_id` (`uuid4().hex[:12]`) threaded through every stage and
the `pipeline_runs` table, for end-to-end lineage.

## Schema changes go through Alembic, not `init_db()`

`src.db.init_db()` still exists as a dev convenience (creates tables straight from the models, no
history) — fine for a throwaway local Postgres, **not** for Supabase/staging/production. For
anything real:

```bash
# after changing a model in src/db.py, src/feature_store/build.py, or src/vector/store.py:
alembic revision --autogenerate -m "add whatever_column"
# review the generated file in alembic/versions/ before applying it
alembic upgrade head
```

`alembic/env.py` always targets `migration_engine` (the direct/session connection), never the
pooled runtime connection.

---

## The scraper

Targets the portal's **Algolia search index REST API** directly — no HTML rendering, no headless
browser. Filters `purpose:"for-sale" AND category.slug:"residential"`, paginates at 25 hits/page,
waits `REQUEST_DELAY_SEC` (1s default) between requests, retries each page up to 3 times with
linear backoff, and uses a 15s HTTP timeout. If a scrape returns 0 listings it raises a
`RuntimeError` and halts the pipeline — that almost always means the site's filters or structure
changed, not that there's genuinely nothing for sale. Output is one JSONL file per run at
`raw_data/listings_raw_<YYYYMMDDTHHMMSSZ>.jsonl`, 22 fields per listing (id, title, price, rooms,
area, geography, agency, etc.), each hit enriched with a `_scraped_at` timestamp and a generated
public `url`.

---

## The database (12 tables)

- **Bronze**: `bronze_listings` — append-only raw JSON, idempotent via a unique
  `(external_id, scraped_at)` constraint and `ON CONFLICT DO NOTHING`.
- **Silver**: `silver_listings` — one cleaned row per ad, keyed by `external_id`, latest-wins
  upsert.
- **Gold**: `locations`, `buildings` (placeholder), `properties` (the canonical entity),
  `listings`, `price_history`.
- **Analytics**: `analysis_properties`, `property_analysis_matches`.
- **Feature store**: `property_features` — one row per Property with everything needed for
  training/serving: type, rooms, baths, area, location, price, `price_per_m2`, outlier flag,
  representative title/agency/photo count (from the newest active listing), active listing count,
  neighbourhood average price/m², and days on market.
- **Vectors**: `property_vectors` — `document_text` + a 2048-dim `embedding` + `dedup_key`.
- **Monitoring**: `pipeline_runs`.

Full column-level schema (types, nullability, defaults, indexes, constraints) lives in
`PROJECT_DOCUMENTATION.md` if you need the complete reference.

---

## Vector embeddings (opt-in)

Runs separately from the main pipeline (`build_vectors.py` or
`docker compose --profile vectors up build-vectors`) and requires `NVIDIA_API_KEY`. Uses the
NVIDIA NIM API (OpenAI-compatible client) with `nvidia/nemotron-3-embed-1b`, producing 2048-dim
vectors — `"passage"` input type for indexing, `"query"` for search.

Each property becomes a **bilingual document**: the raw Arabic title plus extracted signals
rendered in Arabic (floor type, view, building code, phase, furnishing, completion status),
followed by a structured English summary (type/rooms/baths/area, location hierarchy, price and
price/m², a relative neighbourhood comparison shown only when the deviation is ≥5%, days on
market, outlier flag, listing URL).

A `dedup_key` (`city|neighbourhood|property_type|rooms|area_rounded_to_nearest_10`) filters
near-identical properties out of search results at serve time. Embedding is **incremental** — a
property is only re-embedded if it's missing from `property_vectors` or its feature
`computed_at` is newer than its `embedded_at`. Batches of 64, 3 retries with exponential backoff.

> **No HNSW/IVFFlat index on `embedding`**: pgvector's ANN indexes cap out at 2000 dimensions, but
> this model produces 2048. The pipeline uses an exact cosine scan instead — fast and 100% accurate
> at the current scale (~16–50k rows), but worth revisiting past ~100k rows (switch to a ≤1024-dim
> model and add an HNSW index).

---

## Monitoring

Every stage writes to `pipeline_runs`: start/end time, duration, status, and a JSON `stats` blob
(records processed, dropped with reason breakdown, duplicates collapsed, new vs. matched
properties). The telemetry write itself is fault-tolerant — if it fails, the error is logged but
never masks the stage's real exception.

```sql
SELECT stage, status, stats, finished_at - started_at AS duration
FROM pipeline_runs ORDER BY started_at DESC LIMIT 20;
```

`src.monitoring.layer_counts()` gives row counts across `bronze_listings`, `silver_listings`,
`properties`, and `listings` — printed automatically at the end of `data_pipeline.py`.

---

## Resilience

- **Retries**: each DB-touching stage is wrapped with `@with_db_retry` (`src/retry.py`, via
  `tenacity`) for transient `OperationalError`/`DBAPIError` — pooler restarts, brief network blips —
  not bad data. Up to `DB_RETRY_ATTEMPTS` (3) attempts, exponential backoff between
  `DB_RETRY_MIN_WAIT_SECONDS` (1s) and `DB_RETRY_MAX_WAIT_SECONDS` (8s), original exception
  re-raised if retries are exhausted. Each stage commits as a single transaction, so a retry re-runs
  cleanly rather than risking partial writes.
- **Statement timeout**: every connection sets `statement_timeout` (default 60s,
  `DB_STATEMENT_TIMEOUT_MS`) so a stuck query can't hold a pooled connection forever.
- **`pool_pre_ping`**: on for both engines — a dead connection is detected and replaced before it's
  handed to your code.

---

## Logging

Structured logging via `src/logging_config.py`:
- `LOG_FORMAT=text` (default) — `%(asctime)s %(levelname)s [%(name)s] %(message)s`, for local dev.
- `LOG_FORMAT=json` — single-line JSON (`timestamp`, `level`, `logger`, `message`, `exception`), for
  shipping to Datadog/CloudWatch/Grafana Loki in staging/production. Set `ENVIRONMENT=production`
  and `LOG_FORMAT=json` in `.env` for a deployed run.

Noisy third-party loggers (`urllib3`, `sqlalchemy.engine`) are silenced to `WARNING` unless the root
log level is `DEBUG`.

---

## Configuration reference

### Database & connection

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/real_estate` | Runtime connection (port 6543 on Supabase) |
| `DATABASE_MIGRATION_URL` | *(empty — derived from `DATABASE_URL`)* | Migration/DDL connection (direct, port 5432) |
| `DB_POOL_SIZE` | 5 | QueuePool size (migration engine only) |
| `DB_MAX_OVERFLOW` | 5 | Extra connections above pool_size |
| `DB_POOL_TIMEOUT_SECONDS` | 30 | Pool checkout timeout |
| `DB_POOL_RECYCLE_SECONDS` | 1800 | Connection recycle threshold |
| `DB_STATEMENT_TIMEOUT_MS` | 60000 | Postgres statement timeout |
| `DB_CONNECT_TIMEOUT_SECONDS` | 10 | Socket-level connection timeout |
| `DB_SSLMODE` | `"require"` | Postgres TLS mode |
| `DB_RETRY_ATTEMPTS` | 3 | Max retry attempts |
| `DB_RETRY_MIN_WAIT_SECONDS` | 1.0 | Min exponential backoff |
| `DB_RETRY_MAX_WAIT_SECONDS` | 8.0 | Max exponential backoff |

### Application

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `"development"` | `development` / `staging` / `production` |
| `LOG_LEVEL` | `"INFO"` | Python logging level |
| `LOG_FORMAT` | `"text"` | `text` / `json` |

### Scraper

| Variable | Default | Description |
|---|---|---|
| `SCRAPER_APP_ID` | `""` | Algolia Application ID |
| `SCRAPER_API_KEY` | `""` | Algolia search-only API key |
| `SCRAPER_INDEX_NAME` | `""` | Target search index name |
| `SCRAPER_BASE_URL_TEMPLATE` | `""` | API URL template (`{app_id}` placeholder) |
| `SCRAPER_AGENT` | `"listings-scraper-custom"` | HTTP User-Agent |
| `LISTING_URL_TEMPLATE` | `""` | Public listing URL template (`{external_id}` placeholder) |

### Vector embeddings

| Variable | Default | Description |
|---|---|---|
| `NVIDIA_API_KEY` | `""` | NVIDIA NIM API key |
| `NVIDIA_BASE_URL` | `"https://integrate.api.nvidia.com/v1"` | NVIDIA API endpoint |
| `EMBEDDING_MODEL` | `"nvidia/nemotron-3-embed-1b"` | Embedding model name |
| `EMBEDDING_DIM` | 2048 | Vector dimensionality |

---

## Tech stack

| Component | Technology | Version |
|---|---|---|
| Language | Python | 3.12 |
| ORM | SQLAlchemy | 2.0.35 |
| Database | PostgreSQL (Supabase) | 16 |
| Vector extension | pgvector | 0.2.5 |
| DB adapter | psycopg | 3.2.4 |
| Migrations | Alembic | 1.14.1 |
| Config | pydantic-settings | 2.7.1 |
| HTTP client | requests | 2.32.3 |
| Data processing | pandas | 2.2.3 |
| Retry logic | tenacity | 9.0.0 |
| Embedding API client | openai SDK (NVIDIA NIM) | 1.82.0 |
| Containerization | Docker (multi-stage) | — |
| Linter | ruff | 0.11.12 |
| Testing | pytest | 8.3.4 |

---

## Infrastructure & deployment

- **Docker**: multi-stage build on `python:3.12-slim` — `base` (OS deps + Python flags) → `deps`
  (`pip install`) → `app` (copies source, creates non-root `pipeline` user, sets up `raw_data/`).
  Healthcheck runs `get_settings()` to confirm config loads. Entrypoint is `python data_pipeline.py`.
- **docker-compose.yml** services: `migrate` (`alembic upgrade head`), `pipeline`
  (`python data_pipeline.py`, depends on `migrate` succeeding), `build-vectors` (opt-in, `vectors`
  profile). Volume `./raw_data:/app/raw_data` persists scraped files on the host.
- **Makefile**: `install`, `check-db`, `migrate`, `migrate-new msg="..."`, `pipeline`, `vectors`,
  `test`, `lint`, plus `docker-build` / `docker-migrate` / `docker-pipeline`.

---

## Tests

```bash
pytest tests/ -v          # or: make test
ruff check src scripts tests *.py   # or: make lint
```

All pure logic, no live database needed — this is why CI (`.github/workflows/ci.yml`) can run the
full suite plus ruff on every push with zero secrets configured:

| File | Scope |
|---|---|
| `tests/test_matcher.py` | 20+ cases: distance, scoring, hard vetoes, title signals, dynamic area tolerance |
| `tests/test_silver_transform.py` | Field extraction, missing-field handling, all rejection reasons, URL generation, geo bounds |
| `tests/test_document_builder.py` | Bilingual output, missing fields, outlier flag, Arabic title/signals, rich feature inclusion |
| `tests/test_vector_store.py` | Dedup key logic: identical specs, neighbourhood differentiation, area binning |

Representative matcher scenarios: near-identical units ~15m apart with matching specs → merge with
confidence >0.8; different room counts → reject; ~21km apart despite matching specs → reject
(hard distance ceiling); 100m² vs 200m² → reject (area too different); no coordinates → falls back
to attribute-only matching; conflicting building codes / floor types / phases / views → instant
hard-veto reject; matching building code → pass; same GPS pin with no title evidence → reject.

---

## Diagnostic scripts

- **`scripts/check_db.py`** — container healthcheck; runs `SELECT version()` against both the
  runtime (6543) and migration (5432) connections, exit code 0 only if both succeed.
- **`scripts/diagnose_grouping.py`** — sanity-checks entity-resolution candidate grouping: counts
  distinct `property_type` values (warns >30 → likely unnormalized strings), distinct `rooms`
  values (warns >30 → dirty data), distinct `(city, neighbourhood)` pairs, and reports singleton %,
  the 10 largest groups, and average group size.
- **`scripts/print_config.py`** — verifies `.env` exists, flags UTF-8 BOM issues (a common silent
  parsing-failure cause), prints the active environment and redacted DB URLs, and warns if
  `DATABASE_URL` still points at localhost.

---

## Known limitations

- **`Building` stays empty** until there's a real source for building/compound identity — don't
  rely on it yet.
- **Matching thresholds are a first pass** — expect to tune the area%/distance/price-tolerance
  numbers once real duplicate/false-match rates are visible in production.
- **Stale listings don't self-heal**: a listing marked `stale` has no automatic path back to
  `active` if it reappears in a later scrape.
- **pgvector has no ANN index** at 2048 dimensions (see [Vector embeddings](#vector-embeddings-opt-in)) —
  fine today, needs attention past ~100k rows.
- **DB-integration tests** (actually hitting Postgres) are a reasonable next addition — e.g. spin
  up `docker-compose.dev.yml` in CI and run a smaller subset against it.
- Confirm the source portal's ToS permits this data being used commercially.
- Consider Supabase Row Level Security if this database is ever exposed beyond the pipeline itself
  (e.g. a future chatbot reading `property_vectors` directly with an anon key).

## Roadmap

- **Near-term**: a monitoring dashboard (match rate by confidence bucket, alerts when a Property
  exceeds 20 listings), a manual review queue for over-merged Properties, and a backfill script to
  re-resolve existing Properties under the new matcher.
- **Medium-term**: swap Jaccard title similarity for multilingual sentence embeddings; explore
  graph-based clustering (connected components / Louvain) as an alternative to greedy per-listing
  matching; surface uncertain matches (confidence 0.5–0.7) for human labeling toward an eventual ML
  scorer.
- **Long-term**: cross-source matching against other portals; temporal modeling to detect
  re-listings of the same unit after renovation.

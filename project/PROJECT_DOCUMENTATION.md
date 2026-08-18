# Estatio-AI — Real Estate Data Pipeline: Complete Technical Reference

> **Purpose of this document**: This is an internal technical handoff document — NOT a GitHub README.
> It is designed to give a developer or AI agent joining the project's **next stage** a complete,
> self-contained understanding of every data source, database table, pipeline stage, algorithm,
> configuration variable, and infrastructure component in the system.

---

## Table of Contents

1. [Project Overview & Architecture](#1-project-overview--architecture)
2. [Data Source & Scraper](#2-data-source--scraper)
3. [Pipeline Orchestration & Execution Flow](#3-pipeline-orchestration--execution-flow)
4. [Database Architecture (Complete Schema Reference)](#4-database-architecture-complete-schema-reference)
5. [Bronze Layer — Raw Ingestion](#5-bronze-layer--raw-ingestion)
6. [Silver Layer — Cleaning, Validation & Deduplication](#6-silver-layer--cleaning-validation--deduplication)
7. [Gold Layer — Entity Resolution & Canonical Model](#7-gold-layer--entity-resolution--canonical-model)
8. [Feature Store](#8-feature-store)
9. [Vector Embedding Pipeline](#9-vector-embedding-pipeline)
10. [Analysis Matcher (Dashboard Aggregations)](#10-analysis-matcher-dashboard-aggregations)
11. [Monitoring & Observability](#11-monitoring--observability)
12. [Resilience & Retry Policy](#12-resilience--retry-policy)
13. [Configuration Reference (All Environment Variables)](#13-configuration-reference-all-environment-variables)
14. [Infrastructure & Deployment](#14-infrastructure--deployment)
15. [Testing Coverage](#15-testing-coverage)
16. [Diagnostic Scripts](#16-diagnostic-scripts)
17. [Known Constraints & Future Considerations](#17-known-constraints--future-considerations)
18. [File Map](#18-file-map)

---

## 1. Project Overview & Architecture

### What This System Does

Estatio-AI is a **production-grade ETL + entity resolution + vector search pipeline** for the Egyptian real estate market. It:

1. **Scrapes** residential property listings from a major Egyptian real estate portal's Algolia search index API.
2. **Ingests** raw JSON payloads into an append-only Bronze tier.
3. **Cleans & validates** records into a Silver tier with quality gates.
4. **Resolves entities** — matching multiple ads that describe the same physical property into a single canonical `Property` record (Gold tier), even when listings lack unique unit IDs and share compound-level GPS coordinates.
5. **Tracks price history** across time for each listing.
6. **Computes ML features** (feature store) and **generates vector embeddings** (pgvector) for semantic search.
7. **Produces dashboard aggregations** via a secondary analysis matcher.

### Architecture Pattern: Medallion (Bronze → Silver → Gold)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        DATA PIPELINE FLOW                                │
│                                                                          │
│  ┌─────────┐    ┌─────────┐    ┌──────────┐    ┌───────────────────┐    │
│  │ Scraper │───▶│ Bronze  │───▶│  Silver  │───▶│    Gold Layer     │    │
│  │(Algolia)│    │(Raw JSON│    │(Cleaned &│    │(Entity Resolution │    │
│  │         │    │ append) │    │ validated│    │ + Canonical Model)│    │
│  └─────────┘    └─────────┘    └──────────┘    └────────┬──────────┘    │
│                                                         │               │
│                                          ┌──────────────┼───────────┐   │
│                                          ▼              ▼           ▼   │
│                                    ┌──────────┐  ┌───────────┐ ┌──────┐│
│                                    │ Feature  │  │  Analysis │ │Price ││
│                                    │  Store   │  │  Matcher  │ │Hist. ││
│                                    └─────┬────┘  └───────────┘ └──────┘│
│                                          ▼                              │
│                                    ┌──────────┐                         │
│                                    │ Vector   │                         │
│                                    │Embeddings│                         │
│                                    │(pgvector)│                         │
│                                    └──────────┘                         │
└──────────────────────────────────────────────────────────────────────────┘
```

### Technology Stack

| Component | Technology | Version |
|---|---|---|
| Language | Python | 3.12 |
| ORM | SQLAlchemy | 2.0.35 |
| Database | PostgreSQL (Supabase) | 16 |
| Vector Extension | pgvector | 0.2.5 |
| DB Adapter | psycopg | 3.2.4 |
| Migrations | Alembic | 1.14.1 |
| Config Management | pydantic-settings | 2.7.1 |
| HTTP Client | requests | 2.32.3 |
| Data Processing | pandas | 2.2.3 |
| Retry Logic | tenacity | 9.0.0 |
| Embedding API Client | openai (NVIDIA NIM) | 1.82.0 |
| Embedding Model | nvidia/nemotron-3-embed-1b | — |
| Embedding Dimensions | 2048 | — |
| Containerization | Docker (multi-stage) | — |
| Linter | ruff | 0.11.12 |
| Testing | pytest | 8.3.4 |

### Database Connection Architecture

The system maintains **two separate database connections** to work with Supabase's PgBouncer:

| Connection | Port | Pool Strategy | Purpose |
|---|---|---|---|
| **Runtime** (`engine`) | 6543 | `NullPool` (transaction pooler) | All pipeline CRUD operations |
| **Migration** (`migration_engine`) | 5432 | `QueuePool` (direct/session) | DDL, Alembic migrations, extension creation |

The `NullPool` on port 6543 is required because PgBouncer's transaction pooling mode is incompatible with SQLAlchemy's connection pool. The `prepare_threshold: None` setting disables server-side prepared statements for psycopg compatibility with PgBouncer.

---

## 2. Data Source & Scraper

### Source

The scraper targets an Egyptian real estate portal's **Algolia search index REST API**. It does NOT render HTML or use a headless browser — it sends paginated JSON POST requests directly to the underlying search backend.

### Scraper Configuration (Required Environment Variables)

| Variable | Purpose |
|---|---|
| `SCRAPER_APP_ID` | Algolia Application ID |
| `SCRAPER_API_KEY` | Algolia Search-only API key |
| `SCRAPER_INDEX_NAME` | Target search index name |
| `SCRAPER_BASE_URL_TEMPLATE` | API endpoint URL template (contains `{app_id}` placeholder) |
| `LISTING_URL_TEMPLATE` | Public listing URL template (contains `{external_id}` placeholder) |

### Scraper Behavior

- **Filter**: `purpose:"for-sale" AND (category.slug:"residential")` — only residential properties for sale.
- **Pagination**: 25 hits per page, iterates all pages.
- **Rate Limiting**: 1-second delay (`REQUEST_DELAY_SEC`) between requests.
- **Retries**: Up to 3 retries per page with linear backoff (`2 * attempt` seconds).
- **Timeout**: 15-second HTTP timeout per request.
- **User-Agent**: Configurable via `SCRAPER_AGENT` (default: `"listings-scraper-custom"`).

### Fields Retrieved Per Listing (22 attributes)

```
id, externalID, title, title_l1, purpose, price, downPayment, rentFrequency,
rooms, baths, area, plotArea, location, category, geography, furnishingStatus,
completionStatus, createdAt, updatedAt, deal, agency, photoCount
```

### Output Format

Each scrape run produces a single JSONL file at:
```
raw_data/listings_raw_<YYYYMMDDTHHMMSSZ>.jsonl
```

Each line is a JSON object representing one listing hit. The scraper enriches each hit with:
- `_scraped_at`: UTC timestamp string (`%Y%m%dT%H%M%SZ`)
- `url`: Generated from `LISTING_URL_TEMPLATE`

### Safety Rail

If a scrape returns 0 listings, it raises `RuntimeError` to halt the pipeline — this indicates the site structure or filters have changed.

---

## 3. Pipeline Orchestration & Execution Flow

### Entry Point: `data_pipeline.py`

The pipeline is a **6-stage sequential process**. Each stage runs inside an explicit `try/except` block — if any stage fails, the process terminates immediately with `sys.exit(1)` to prevent partial/corrupted downstream outputs.

### Execution Stages

| Stage | Function | Description |
|---|---|---|
| **1/6** | `scrape_all(out_dir="raw_data")` | Scrapes Algolia API → produces JSONL file |
| **2/6** | `ingest_bronze(raw_dir="raw_data", run_id=run_id)` | Loads JSONL → appends to `bronze_listings` table; archives processed files to `raw_data/_ingested/` |
| **3/6** | `transform_silver(run_id=run_id)` | Cleans, validates, deduplicates → upserts into `silver_listings` |
| **4/6** | `build_gold()` | Entity resolution → canonical `properties`, `listings`, `price_history`; marks stale ads |
| **5/6** | `build_features()` | Computes ML features → upserts into `property_features` |
| **6/6** | `run_analysis_matcher(run_id=run_id)` | Dashboard aggregation → `analysis_properties` + `property_analysis_matches` |

### Vector Pipeline (Separate, Opt-in)

Vector embeddings are built separately via `build_vectors.py` (or `docker compose --profile vectors up build-vectors`). This stage requires an `NVIDIA_API_KEY` and calls the NVIDIA NIM embedding API.

### Run ID

Every pipeline execution generates a unique 12-character hex identifier (`run_id = uuid4().hex[:12]`) for end-to-end lineage tracking across all stages and the `pipeline_runs` monitoring table.

---

## 4. Database Architecture (Complete Schema Reference)

The system uses **12 database tables** across 5 logical groups. All timestamps are timezone-aware UTC.

### 4.1 Entity-Relationship Overview

```
┌───────────────┐     ┌───────────────┐     ┌───────────────┐
│bronze_listings│────▶│silver_listings│────▶│  locations     │
│  (raw JSON)   │     │  (cleaned)    │     │  (city+nbhd)  │
└───────────────┘     └───────────────┘     └───────┬───────┘
                                                    │
                            ┌───────────────────────┼──────────────┐
                            ▼                       ▼              ▼
                      ┌──────────┐           ┌───────────┐  ┌──────────┐
                      │buildings │           │properties │  │analysis_ │
                      │(optional)│           │(canonical)│  │properties│
                      └────┬─────┘           └─────┬─────┘  └────┬─────┘
                           │                       │              │
                           └───────┐               │              │
                                   ▼               ▼              ▼
                              ┌─────────┐   ┌──────────┐  ┌──────────────────┐
                              │         │   │ listings  │  │property_analysis_│
                              │         │   │  (ads)    │  │    matches       │
                              │         │   └────┬──────┘  └──────────────────┘
                              │         │        │
                              │         │        ▼
                              │         │  ┌───────────┐
                              │         │  │  price_    │
                              │         │  │  history   │
                              │         │  └───────────┘
                              │         │
                      ┌───────┴─────────┴────────┐
                      │    property_features      │
                      │   (ML feature store)      │
                      └───────────┬───────────────┘
                                  ▼
                      ┌───────────────────────┐
                      │   property_vectors    │
                      │   (pgvector 2048-d)   │
                      └───────────────────────┘

                      ┌───────────────────────┐
                      │    pipeline_runs      │
                      │   (monitoring/audit)  │
                      └───────────────────────┘
```

---

### 4.2 Table: `bronze_listings` (Bronze Layer)

**Purpose**: Append-only landing zone for raw, unparsed JSON scrape payloads.

| Column | Type | PK | Nullable | Default | Index | Notes |
|---|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — | — |
| `source` | String | — | No | `"portal_eg"` | — | Data source identifier |
| `external_id` | String | — | No | — | ✅ | Portal's ad ID |
| `raw_data` | JSONB | — | No | — | — | Complete raw JSON payload |
| `scraped_at` | DateTime(tz) | — | No | — | — | When the portal was scraped |
| `ingested_at` | DateTime(tz) | — | No | `now()` | — | When row was written to DB |
| `processed` | Boolean | — | No | `False` | ✅ | Flag: consumed by Silver? |
| `run_id` | String | — | Yes | — | ✅ | Pipeline execution lineage |

**Constraints**:
- `uq_bronze_external_scraped`: UNIQUE on `(external_id, scraped_at)` — makes re-ingestion of the same JSONL file idempotent via `ON CONFLICT DO NOTHING`.

---

### 4.3 Table: `silver_listings` (Silver Layer)

**Purpose**: One cleaned, validated row per ad (keyed by `external_id`). Latest-wins upsert semantics.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `external_id` | String | ✅ | No | — | Portal's ad ID |
| `url` | String | — | Yes | — | Generated public listing URL |
| `title` | String | — | Yes | — | Ad title (Arabic) |
| `purpose` | String | — | Yes | — | e.g. `"for-sale"` |
| `price_egp` | Float | — | Yes | — | Price in Egyptian Pounds |
| `down_payment_egp` | Float | — | Yes | — | Down payment amount |
| `rooms` | Float | — | Yes | — | Bedroom count |
| `baths` | Float | — | Yes | — | Bathroom count |
| `area_m2` | Float | — | Yes | — | Built-up area in m² |
| `plot_area_m2` | Float | — | Yes | — | Land plot area in m² |
| `property_type` | String | — | Yes | — | e.g. `"Apartments"`, `"شاليهات"` |
| `city` | String | — | Yes | — | City name |
| `neighbourhood` | String | — | Yes | — | Neighbourhood name |
| `province` | String | — | Yes | — | Province/governorate |
| `lat` | Float | — | Yes | — | Latitude |
| `lng` | Float | — | Yes | — | Longitude |
| `furnishing_status` | String | — | Yes | — | e.g. `"furnished"`, `"unfurnished"` |
| `completion_status` | String | — | Yes | — | e.g. `"ready"`, `"off-plan"` |
| `agency_name` | String | — | Yes | — | Real estate agency/broker |
| `photo_count` | Integer | — | Yes | — | Number of listing photos |
| `scraped_at` | DateTime(tz) | — | Yes | — | Inherited from Bronze |
| `updated_at` | DateTime(tz) | — | No | `now()` | Auto-updated on change |

---

### 4.4 Table: `locations` (Gold Layer)

**Purpose**: Normalized geographic lookup table. Canonical `(city, neighbourhood)` pairs.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `city` | String | — | No | — | City name |
| `neighbourhood` | String | — | Yes | — | Neighbourhood name |
| `province` | String | — | Yes | — | Province/governorate |

**Constraints**:
- `uq_location_city_neighbourhood`: UNIQUE on `(city, neighbourhood)`.

---

### 4.5 Table: `buildings` (Gold Layer)

**Purpose**: Grouping placeholder for compound/tower entities (currently a scaffold for future use).

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `location_id` | Integer (FK→locations.id) | — | Yes | — | — |
| `name` | String | — | Yes | — | Building/compound name |

---

### 4.6 Table: `properties` (Gold Layer — **Core Entity**)

**Purpose**: Canonical physical real estate unit. One row per unique physical property, resolved from matching one or more listing ads.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `location_id` | Integer (FK→locations.id) | — | No | — | — |
| `building_id` | Integer (FK→buildings.id) | — | Yes | — | — |
| `property_type` | String | — | Yes | — | Apartment, Villa, etc. |
| `rooms` | Float | — | Yes | — | Room count |
| `baths` | Float | — | Yes | — | Bath count |
| `area_m2` | Float | — | Yes | — | Built-up area |
| `lat` | Float | — | Yes | — | Latitude |
| `lng` | Float | — | Yes | — | Longitude |
| `match_method` | String | — | No | `"new"` | `"new"` / `"scored_match"` / `"split"` |
| `match_confidence` | Float | — | No | `1.0` | Confidence score [0.0–1.0] |
| `current_price_egp` | Float | — | Yes | — | Min active listing price |
| `price_per_m2` | Float | — | Yes | — | Computed: price / area |
| `is_outlier` | Boolean | — | No | `False` | Statistical pricing outlier flag |
| `created_at` | DateTime(tz) | — | No | `now()` | — |

**Indexes**: `ix_properties_location_id` on `location_id`.

**Relationships**: `properties.listings` → one-to-many with `listings`.

---

### 4.7 Table: `listings` (Gold Layer)

**Purpose**: Active listing (ad) entity linking back to a single physical Property.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `external_id` | String | — | No | — | UNIQUE, indexed |
| `source` | String | — | No | `"portal_eg"` | — |
| `property_id` | Integer (FK→properties.id) | — | No | — | Canonical property link |
| `url` | String | — | Yes | — | Public listing URL |
| `title` | String | — | Yes | — | Ad title (Arabic) |
| `furnishing_status` | String | — | Yes | — | — |
| `completion_status` | String | — | Yes | — | — |
| `agency_name` | String | — | Yes | — | — |
| `photo_count` | Integer | — | Yes | — | — |
| `current_price_egp` | Float | — | Yes | — | — |
| `status` | String | — | No | `"active"` | `"active"` / `"stale"` |
| `first_seen_at` | DateTime(tz) | — | No | `now()` | — |
| `last_seen_at` | DateTime(tz) | — | No | `now()` | — |

**Relationships**:
- `listings.property` → many-to-one with `properties`
- `listings.price_history` → one-to-many with `price_history`

---

### 4.8 Table: `price_history`

**Purpose**: Append-only price tracking. A new row is inserted each time a listing's price changes between pipeline runs.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `listing_id` | Integer (FK→listings.id) | — | No | — | Indexed |
| `price_egp` | Float | — | No | — | — |
| `observed_at` | DateTime(tz) | — | No | `now()` | — |

---

### 4.9 Table: `pipeline_runs` (Monitoring)

**Purpose**: Observability log for each pipeline stage execution.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `run_id` | String | — | No | — | Indexed |
| `stage` | String | — | No | — | `"bronze"` / `"silver"` / `"gold"` / etc. |
| `started_at` | DateTime(tz) | — | No | — | — |
| `finished_at` | DateTime(tz) | — | Yes | — | — |
| `status` | String | — | No | `"running"` | `"running"` / `"success"` / `"failed"` |
| `stats` | JSONB | — | No | `{}` | Arbitrary metrics (records processed, dropped, etc.) |
| `error` | String | — | Yes | — | Exception message on failure |

---

### 4.10 Table: `analysis_properties` (Dashboard Analytics)

**Purpose**: Stable analytics entity grouping properties by `(location_id, property_type, rooms)` for dashboard trend tracking.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `location_id` | Integer (FK→locations.id) | — | No | — | — |
| `property_type` | String | — | Yes | — | — |
| `rooms` | Float | — | Yes | — | — |
| `created_at` | DateTime(tz) | — | No | `now()` | — |

**Constraints**: `uq_analysis_property_key`: UNIQUE on `(location_id, property_type, rooms)`.

---

### 4.11 Table: `property_analysis_matches`

**Purpose**: Audit map connecting Silver listings to analytics groups per pipeline run.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `id` | Integer | ✅ | No | autoincrement | — |
| `run_id` | String | — | No | — | Indexed |
| `silver_external_id` | String | — | No | — | Indexed |
| `analysis_property_id` | Integer (FK→analysis_properties.id) | — | Yes | — | — |
| `confidence` | Float | — | No | `0.0` | — |
| `method` | String | — | No | `"new"` | `"new"` / `"scored_match"` |
| `created_at` | DateTime(tz) | — | No | `now()` | — |

**Constraints**: `uq_run_silver_external`: UNIQUE on `(run_id, silver_external_id)`.

---

### 4.12 Table: `property_features` (Feature Store)

**Purpose**: Pre-computed ML & search features per canonical Property.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `property_id` | Integer | ✅ | No | — | — |
| `property_type` | String | — | Yes | — | — |
| `rooms` | Float | — | Yes | — | — |
| `baths` | Float | — | Yes | — | — |
| `area_m2` | Float | — | Yes | — | — |
| `city` | String | — | Yes | — | — |
| `neighbourhood` | String | — | Yes | — | — |
| `province` | String | — | Yes | — | — |
| `price_egp` | Float | — | Yes | — | — |
| `price_per_m2` | Float | — | Yes | — | — |
| `is_outlier` | Boolean | — | No | `False` | — |
| `representative_title` | String | — | Yes | — | Title from newest active listing |
| `furnishing_status` | String | — | Yes | — | From newest active listing |
| `completion_status` | String | — | Yes | — | From newest active listing |
| `url` | String | — | Yes | — | From newest active listing |
| `agency_name` | String | — | Yes | — | From newest active listing |
| `photo_count` | Integer | — | Yes | — | From newest active listing |
| `active_listing_count` | Integer | — | No | `0` | Count of active ads |
| `neighbourhood_avg_price_per_m2` | Float | — | Yes | — | Contextual benchmark |
| `days_on_market` | Integer | — | Yes | — | Days since earliest active listing |
| `computed_at` | DateTime(tz) | — | Yes | — | Feature computation timestamp |

---

### 4.13 Table: `property_vectors` (Vector Embeddings)

**Purpose**: Dense vector embeddings for semantic property search via pgvector.

| Column | Type | PK | Nullable | Default | Notes |
|---|---|---|---|---|---|
| `property_id` | Integer | ✅ | No | — | — |
| `document_text` | String | — | No | — | Natural language document fed to embedder |
| `embedding` | Vector(2048) | — | No | — | pgvector dense embedding |
| `embedded_at` | DateTime(tz) | — | No | — | — |
| `dedup_key` | String | — | Yes | — | Serve-time dedup filter |

> **No index on `embedding` column**: pgvector HNSW/IVFFlat indexes are capped at 2000 dimensions, but the embedding model produces 2048-dimensional vectors. For the current dataset size (~16–50k rows), an exact cosine scan is both fast and 100% accurate. If data grows past ~100k rows, switch to a ≤1024-dim model and add an HNSW index.

---

### 4.14 Schema Migration History

| Revision | Description |
|---|---|
| `0001` | Initial squashed schema: all 12 tables, pgvector extension, HNSW note |
| `0002` | Bronze idempotency: adds `run_id` column + `uq_bronze_external_scraped` unique constraint |

---

## 5. Bronze Layer — Raw Ingestion

### Module: `src/bronze/ingest.py`

### Behavior

1. Discovers all `*.jsonl` files in `raw_data/` (sorted alphabetically).
2. Parses each file line-by-line as JSON.
3. For each JSON hit:
   - Extracts `external_id` from `hit["externalID"]` or `hit["id"]`.
   - Extracts `scraped_at` from `hit["_scraped_at"]` (parsed as `%Y%m%dT%H%M%SZ` UTC) or defaults to `now()`.
   - Inserts into `bronze_listings` with `source="portal_eg"`, `raw_data=hit`, and the active `run_id`.
   - Uses `ON CONFLICT DO NOTHING` on the `(external_id, scraped_at)` unique constraint → **idempotent re-ingestion**.
4. After successful ingestion, the pipeline moves processed JSONL files to `raw_data/_ingested/`.

### Key Design Decision

Bronze is append-only. Every observation of a listing at a specific scrape time is preserved. This allows reconstructing the complete history of how a listing changed over time from raw data.

---

## 6. Silver Layer — Cleaning, Validation & Deduplication

### Module: `src/silver/transform.py`

### Batching Configuration

| Constant | Value | Purpose |
|---|---|---|
| `PAGE_SIZE` | 5000 | Batch size for querying unprocessed bronze rows (prevents memory bloat) |
| `UPSERT_BATCH_SIZE` | 1000 | Batch size for multi-row PostgreSQL upserts |

### Field Mapping (Raw JSON → Silver)

| Silver Column | Source JSON Path | Parsing Logic |
|---|---|---|
| `external_id` | `externalID` or `id` | `str()` conversion |
| `url` | Generated | `LISTING_URL_TEMPLATE.replace("{external_id}", ...)` |
| `title` | `title` | Direct |
| `purpose` | `purpose` | Direct (e.g. `"for-sale"`) |
| `price_egp` | `price` | Direct (Float) |
| `down_payment_egp` | `downPayment` | Direct |
| `rooms` | `rooms` | Direct (Float) |
| `baths` | `baths` | Direct |
| `area_m2` | `area` | Direct |
| `plot_area_m2` | `plotArea` | Direct |
| `property_type` | `category[-1]["name"]` | Last item in category hierarchy list |
| `city` | `location` where `type=="city"` → `name` | Extracted from location array |
| `neighbourhood` | `location` where `type=="neighbourhood"` → `name` | Extracted from location array |
| `province` | `location` where `type=="province"` → `name` | Extracted from location array |
| `lat` | `geography.lat` | Direct |
| `lng` | `geography.lng` | Direct |
| `furnishing_status` | `furnishingStatus` | Direct |
| `completion_status` | `completionStatus` | Direct |
| `agency_name` | `agency.name` | Direct |
| `photo_count` | `photoCount` | Direct |
| `scraped_at` | Inherited from Bronze row | Direct |

### Data Quality Validation Rules

Records failing any rule are **dropped** (not inserted into Silver) and counted in monitoring metrics:

| Rule Name | Condition | Purpose |
|---|---|---|
| `missing_price_and_area` | Both `price_egp` and `area_m2` missing/zero | Drop useless records |
| `missing_price` | `price_egp` is missing/zero | — |
| `missing_area` | `area_m2` is missing/zero | — |
| `price_suspiciously_low` | `price_egp < 10,000` | Filters test/noise listings |
| `price_suspiciously_high` | `price_egp > 500,000,000` | Filters implausible values |
| `area_suspiciously_large` | `area_m2 > 10,000` | — |
| `negative_rooms` | `rooms < 0` | — |
| `rooms_suspiciously_high` | `rooms > 20` | — |
| `coordinates_outside_egypt` | `lat` not in [22.0°, 31.7°] OR `lng` not in [24.7°, 36.9°] | Geo bounding box check |

### Upsert Semantics

Silver uses `ON CONFLICT (external_id) DO UPDATE` with a **temporal guard**:
- Existing rows are only updated if the incoming `scraped_at` is newer than the existing `scraped_at`.
- This guarantees idempotency even if bronze pages are processed out of order.

### Intra-Page Deduplication

Within each bronze page, if multiple records share the same `external_id`, only the record with the latest `scraped_at` is kept.

---

## 7. Gold Layer — Entity Resolution & Canonical Model

### The Core Problem

Egyptian real estate portals **lack unique physical unit IDs**. Multiple agents post separate ads for the same physical apartment. Large developer compounds (e.g., Madinaty, Palm Hills, El Rehab) assign **compound-level marketing GPS coordinates** to all units, so hundreds of physically distinct apartments share identical lat/lng. The Gold layer must resolve which ads describe the same physical property.

### Design Evolution

| Version | Approach | Problem |
|---|---|---|
| v1 | Exact `type+rooms`, area ±5%, geo ≤50m | Over-merged in compounds (shared GPS pins) |
| v2 | Added price-similarity guard | Still over-merged (same floor plans, same prices) |
| v3 | Multi-candidate scoring + title signal extraction | Improved but missed aggregated signal conflicts |
| v4 (current) | Aggregated title signals + hard distance ceiling + compound centroid guard | Production-ready |

### Entity Resolution Algorithm (`src/gold/matcher.py`)

#### Step 1: Candidate Indexing

Candidates are pre-indexed by `(location_id, property_type, rooms)`. This narrows the search space from N total properties to ~50 candidates per listing (vs ~500 in earlier versions), reducing comparisons for 48,000 listings from 24M to 2.4M.

#### Step 2: Hard Disqualification Gates (evaluated first — instant reject)

| Gate | Condition | Rationale |
|---|---|---|
| Property type mismatch | `property_type ≠ candidate.property_type` | Different property types cannot be same unit |
| Room count mismatch | `rooms ≠ candidate.rooms` | Different room counts → different units |
| Area deviation | `area_diff_pct > dynamic_tolerance` | See dynamic tolerance table below |
| Price hard reject | `price_diff_pct > 50%` | Prices differing by >50% → different units |
| Hard title conflict | Building, phase, or floor type mismatch | e.g., B6 ≠ B11, Phase 5 ≠ Phase 7 |
| Garden/ground vs roof | Ground+garden unit cannot match roof/penthouse | Structurally incompatible |
| View conflict (chalets) | Sea view ≠ lagoon view ≠ golf view | For chalets/villas only |
| Distance ceiling | `distance > 1000m` | Different location entirely |
| Compound centroid guard | `distance < 5m` AND no building/phase title evidence | Prevents merging at marketing pins |

#### Dynamic Area Tolerance

| Condition | Tolerance |
|---|---|
| No coordinates available | 5% |
| With coordinates, area ≤ 100 m² | 5% |
| With coordinates, 100 < area ≤ 200 m² | 3% |
| With coordinates, area > 200 m² | 2% |

#### Step 3: Weighted Scoring (for candidates passing all gates)

$$\text{Confidence} = \frac{25 \cdot S_{\text{type}} + 15 \cdot S_{\text{rooms}} + 20 \cdot S_{\text{area}} + 10 \cdot S_{\text{geo}} + 15 \cdot S_{\text{price}} + 15 \cdot S_{\text{title}}}{100}$$

| Feature | Weight | Score Calculation |
|---|---|---|
| **Property Type** | 25 | 1.0 (always, since hard gate already filters) |
| **Rooms** | 15 | 1.0 (always, since hard gate already filters) |
| **Area** | 20 | `max(0, 1.0 - area_diff_pct / area_tolerance)` (linear decay) |
| **Geo Distance** | 10 | `max(0, 1.0 - distance / 50m)` (linear decay to 50m); fallback 0.5 if coords missing |
| **Price** | 15 | `max(0, 1.0 - price_diff_pct / 15%)` (linear decay); fallback 0.5 if price missing |
| **Title** | 15 | Base 1.0, penalized by 0.6 for view mismatch |

#### Step 4: Best Candidate Selection

- All candidates are scored (not greedy first-match).
- The candidate with the highest confidence is selected.
- **Minimum threshold**: `confidence ≥ 0.80` to accept a match.
- Below threshold → a new `Property` record is created.

> **Design Philosophy**: False negatives (creating a new Property for an existing one) are preferred over false positives (corrupting a property cluster by merging distinct units). A duplicate property is harmless; a corrupted property pollutes price history permanently.

### Title Signal Extraction (`extract_title_signals`)

Arabic ad titles contain critical unit-level information extracted via regex and keyword matching:

| Signal | Examples | Regex / Keywords |
|---|---|---|
| **Floor Type** | Roof (روف), Ground (ارضي), Garden (جاردن), Penthouse (بنتهاوس), Duplex (دوبلكس), Corner (كورنر), Typical (دور متكرر) | Arabic keyword dictionary |
| **View Type** | Sea view (سي فيو), Lagoon (لاجون), Golf (جولف), Landscape (لاند سكيب), Club (كلوب هاوس) | Arabic keyword dictionary |
| **Phase** | Phase 5 (المرحلة 5), Group 111 (مجموعة 111), Block B (بلوك), Cluster 3, Zone | Regex: `(?:المرحلة\|منطقة\|مرحلة\|phase\|مجموعة\|group\|بلوك\|block\|كلاستر\|cluster\|زون\|zone)\s*([\w\u0600-\u06FF]+)` |
| **Building Code** | B6, B12, C1 | Regex: `(?<![a-zA-Z0-9])([a-zA-Z]\d{1,3})(?![a-zA-Z0-9])` + Arabic fallback |

### Aggregated Title Signals

When evaluating a candidate, signals are aggregated across **all** listings already attached to that property. This means if Property #100 has listings mentioning B6 and B8, a new listing mentioning B11 will hard-reject against both B6 and B8 — even though B6 and B8 were accepted together.

### Text Normalization

All Arabic text is normalized before comparison:
1. Unicode NFKC normalization
2. Strip Arabic diacritics (`[\u064B-\u065F\u0670\u06D6-\u06ED]`)
3. Remove Tatweel character (`\u0640`)
4. Lowercase + collapse whitespace

### Over-Merge Repair: Split Pass

At the start of each Gold build, `_split_over_merged_properties()` scans existing properties for contradictory title signals:
1. For properties with ≥2 listings, groups listings by `(building, phase)`.
2. If >1 distinct group exists, keeps the largest group on the existing property and creates new properties (with `match_method="split"`) for each smaller group.

### Stale Listing Detection

After entity resolution, listings present in the database but absent from the current Silver data are marked `status="stale"`.

### Aggregate Computation

After matching:
1. `current_price_egp` = minimum price among all **active** listings for each property.
2. `price_per_m2` = `current_price_egp / area_m2`.
3. Outlier detection using 1st and 99th percentiles of `price_per_m2`:
   $$\text{is\_outlier} = (\text{price\_per\_m2} < q_{0.01}) \lor (\text{price\_per\_m2} > q_{0.99})$$

---

## 8. Feature Store

### Module: `src/feature_store/build.py`

### Purpose

Computes a denormalized, pre-joined feature row per canonical Property — the single source of truth for downstream vector embedding and ML training.

### Feature Engineering Details

| Feature | Computation |
|---|---|
| `representative_title` | Title from the **newest active** listing (by `last_seen_at`) |
| `furnishing_status` | From newest active listing |
| `completion_status` | From newest active listing |
| `url` | From newest active listing |
| `agency_name` | From newest active listing |
| `photo_count` | From newest active listing |
| `active_listing_count` | Count of listings with `status="active"` |
| `days_on_market` | `(now - min(first_seen_at across active listings)).days` |
| `neighbourhood_avg_price_per_m2` | Mean `price_per_m2` across all properties in the same `(city, neighbourhood)` |

### Upsert Strategy

Batched PostgreSQL `ON CONFLICT (property_id) DO UPDATE` in chunks of 1000.

---

## 9. Vector Embedding Pipeline

### Overview

The vector pipeline is **opt-in** and runs separately from the main pipeline. It converts `PropertyFeatures` rows into dense 2048-dimensional vector embeddings stored in PostgreSQL via pgvector.

### Embedding Model

| Parameter | Value |
|---|---|
| Provider | NVIDIA NIM API |
| Model | `nvidia/nemotron-3-embed-1b` |
| Dimensions | 2048 |
| Base URL | `https://integrate.api.nvidia.com/v1` |
| API Client | OpenAI Python SDK (compatible endpoint) |
| Input Type (indexing) | `"passage"` |
| Input Type (search) | `"query"` |

### Document Construction (`src/vector/document_builder.py`)

Each property is converted into a **bilingual natural language document** optimized for dense retrieval:

**Arabic Segment** (from `representative_title`):
- Raw Arabic title preserved for semantic alignment with Arabic queries
- Extracted signals rendered in Arabic: floor type (أرضي, روف, بنتهاوس...), view type (فيو بحر, فيو لاجون...), building code (بلوك B6), phase (مرحلة 5), furnishing (مفروش, غير مفروش), completion (جاهز للتسليم, تحت الإنشاء)

**English Segment** (structured summary):
- Property type, rooms, baths, area
- Location hierarchy (neighbourhood, city, province)
- Furnishing and completion status
- Agency name
- Price and price per m²
- Relative neighbourhood comparison (e.g., "25% above neighbourhood average of 20,000 EGP/m²") — only shown when deviation ≥ 5%
- Days on market
- Statistical outlier flag
- Listing URL

### Serve-Time Deduplication Key

To prevent near-identical properties from cluttering search results:

$$\text{area\_rounded} = \text{round}\left(\frac{\text{area\_m2}}{10}\right) \times 10$$

$$\text{dedup\_key} = \text{city}|\text{neighbourhood}|\text{property\_type}|\text{rooms}|\text{area\_rounded}$$

### Incremental Embedding

The vector builder only re-embeds properties where:
- The property is missing from `property_vectors`, OR
- The feature `computed_at` timestamp is newer than the existing `embedded_at` timestamp

### Batch Processing

Embeddings are generated in batches of 64, with retry logic (3 attempts, exponential backoff) for API resilience.

---

## 10. Analysis Matcher (Dashboard Aggregations)

### Module: `src/gold/analysis_matcher.py`

### Purpose

While the Gold entity matcher targets **high precision** (strict matching to avoid corrupting price histories), the analysis matcher provides **high recall** for dashboard analytics. It groups all listings by `(location_id, property_type, rooms)` without title intelligence, distance limits, or price guards.

### Comparison: Entity Matcher vs Analysis Matcher

| Dimension | Entity Matcher (`matcher.py`) | Analysis Matcher (`analysis_matcher.py`) |
|---|---|---|
| Objective | High precision physical unit resolution | High recall analytical grouping |
| Grouping Key | `(location_id, property_type, rooms)` + multi-signal scoring | `(location_id, property_type, rooms)` exact match |
| Title Intelligence | Full parsing (floor, view, phase, building) | None |
| Distance Ceiling | 1000m hard limit; <5m centroid enforcement | None |
| Price Guard | 50% hard reject + 15% soft scale | None |
| Confidence Threshold | ≥ 0.80 | Fixed at 1.0 |
| Output Tables | `properties`, `listings`, `price_history` | `analysis_properties`, `property_analysis_matches` |

### Stable Identity

`AnalysisProperty` IDs persist across pipeline runs (enforced by the UNIQUE constraint on the grouping key), preserving historical dashboard metrics.

---

## 11. Monitoring & Observability

### Module: `src/monitoring.py`

### `track_stage` Context Manager

Every pipeline stage wraps its execution in `track_stage(run_id, stage_name)`:
1. Records `started_at` timestamp.
2. Yields a mutable `stats` dictionary for the stage to populate.
3. On completion/failure, computes `duration_seconds` and persists a `PipelineRun` row.
4. Fault-tolerant: if telemetry DB write fails, it logs the error but does NOT mask the primary exception.

### `layer_counts()` Helper

Returns row counts across all 4 pipeline tables (`bronze_listings`, `silver_listings`, `properties`, `listings`) for quick health checks.

### Logging (`src/logging_config.py`)

| Mode | Format | Use Case |
|---|---|---|
| `text` | `%(asctime)s %(levelname)s [%(name)s] %(message)s` | Local development |
| `json` | Single-line JSON with `timestamp`, `level`, `logger`, `message`, `exception` | Production log aggregation (Datadog, CloudWatch, Grafana Loki) |

Noisy third-party loggers (`urllib3`, `sqlalchemy.engine`) are silenced to `WARNING` unless root log level is `DEBUG`.

---

## 12. Resilience & Retry Policy

### Module: `src/retry.py`

### `@with_db_retry` Decorator

Applied to pipeline functions that perform database operations:

| Parameter | Value |
|---|---|
| Target Exceptions | `OperationalError`, `DBAPIError` |
| Max Attempts | `DB_RETRY_ATTEMPTS` (default: 3) |
| Backoff Strategy | Exponential: min `DB_RETRY_MIN_WAIT_SECONDS` (1s), max `DB_RETRY_MAX_WAIT_SECONDS` (8s) |
| Reraise | `True` (original exception re-raised after exhausting retries) |

Designed to handle transient Supabase PgBouncer restarts, network hiccups, and connection pool resets.

---

## 13. Configuration Reference (All Environment Variables)

### Database & Connection

| Variable | Type | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | str | `postgresql+psycopg://postgres:postgres@localhost:5432/real_estate` | Runtime connection (transaction pooler, port 6543 for Supabase) |
| `DATABASE_MIGRATION_URL` | str | `""` | Migration/DDL connection (direct, port 5432); auto-derived from `DATABASE_URL` if empty |
| `DB_POOL_SIZE` | int | 5 | QueuePool size (migration engine only) |
| `DB_MAX_OVERFLOW` | int | 5 | Extra connections above pool_size |
| `DB_POOL_TIMEOUT_SECONDS` | int | 30 | Pool checkout timeout |
| `DB_POOL_RECYCLE_SECONDS` | int | 1800 | Connection recycle threshold (30 min) |
| `DB_STATEMENT_TIMEOUT_MS` | int | 60000 | PostgreSQL statement timeout (60s) |
| `DB_CONNECT_TIMEOUT_SECONDS` | int | 10 | Socket-level connection timeout |
| `DB_SSLMODE` | str | `"require"` | PostgreSQL TLS mode |
| `DB_RETRY_ATTEMPTS` | int | 3 | Max retry attempts |
| `DB_RETRY_MIN_WAIT_SECONDS` | float | 1.0 | Min exponential backoff |
| `DB_RETRY_MAX_WAIT_SECONDS` | float | 8.0 | Max exponential backoff |

### Application

| Variable | Type | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | Literal | `"development"` | `"development"` / `"staging"` / `"production"` |
| `LOG_LEVEL` | str | `"INFO"` | Python logging level |
| `LOG_FORMAT` | Literal | `"text"` | `"text"` / `"json"` |

### Scraper

| Variable | Type | Default | Description |
|---|---|---|---|
| `SCRAPER_APP_ID` | str | `""` | Algolia Application ID |
| `SCRAPER_API_KEY` | str | `""` | Algolia Search API key |
| `SCRAPER_INDEX_NAME` | str | `""` | Target search index name |
| `SCRAPER_BASE_URL_TEMPLATE` | str | `""` | API URL template (`{app_id}` placeholder) |
| `SCRAPER_AGENT` | str | `"listings-scraper-custom"` | HTTP User-Agent |
| `LISTING_URL_TEMPLATE` | str | `""` | Public listing URL template (`{external_id}` placeholder) |

### Vector Embeddings

| Variable | Type | Default | Description |
|---|---|---|---|
| `NVIDIA_API_KEY` | str | `""` | NVIDIA NIM API key |
| `NVIDIA_BASE_URL` | str | `"https://integrate.api.nvidia.com/v1"` | NVIDIA API endpoint |
| `EMBEDDING_MODEL` | str | `"nvidia/nemotron-3-embed-1b"` | Embedding model name |
| `EMBEDDING_DIM` | int | 2048 | Vector dimensionality |

### Validation

The config system (`pydantic-settings`) rejects placeholder passwords — if `DATABASE_URL` or `DATABASE_MIGRATION_URL` contains `[YOUR-PASSWORD]`, a `ValueError` is raised at startup.

---

## 14. Infrastructure & Deployment

### Docker (Multi-Stage Build)

**Base Image**: `python:3.12-slim`

**Stages**:
1. `base`: OS packages (`libpq5`, `curl`), Python environment flags
2. `deps`: `pip install -r requirements.txt`
3. `app`: Copy source, create non-root `pipeline` user (UID 1000), set up `raw_data/` directories

**Healthcheck**: `python -c "from src.config import get_settings; get_settings(); print('ok')"`

**Entrypoint**: `python data_pipeline.py`

### Docker Compose Services

| Service | Command | Profile | Dependencies |
|---|---|---|---|
| `migrate` | `alembic upgrade head` | default | — |
| `pipeline` | `python data_pipeline.py` | default | `migrate` (must complete successfully) |
| `build-vectors` | `python build_vectors.py` | `vectors` (opt-in) | — |

**Volume**: `./raw_data:/app/raw_data` (persists scraped files on host)

### Development Environment (`docker-compose.dev.yml`)

Provides a local PostgreSQL 16 instance with pgvector:
- Image: `pgvector/pgvector:pg16`
- Credentials: `postgres:postgres`
- Database: `real_estate`
- Port: `5432`
- Persistent volume: `postgres_data`

### Makefile Targets

| Target | Command | Description |
|---|---|---|
| `install` | `pip install -r requirements.txt` | Install dependencies |
| `check-db` | `python scripts/check_db.py` | Test both DB connections |
| `migrate` | `alembic upgrade head` | Apply migrations |
| `migrate-new` | `alembic revision --autogenerate -m "$(msg)"` | Generate new migration |
| `pipeline` | `python data_pipeline.py` | Run full pipeline |
| `vectors` | `python build_vectors.py` | Build vector embeddings |
| `test` | `pytest tests/ -v` | Run test suite |
| `lint` | `python -m ruff check src scripts tests *.py` | Lint codebase |
| `docker-build` | `docker build -t real-estate-pipeline .` | Build Docker image |
| `docker-migrate` | `docker compose run --rm migrate` | Run migrations in Docker |
| `docker-pipeline` | `docker compose run --rm pipeline` | Run pipeline in Docker |

---

## 15. Testing Coverage

### Test Files & Scope

| File | Scope | Key Test Cases |
|---|---|---|
| `tests/test_matcher.py` | Entity resolution algorithm | 20+ test cases covering distance, scoring, hard vetoes, title signals, dynamic area tolerance |
| `tests/test_silver_transform.py` | Silver data cleaning & validation | Field extraction, missing field handling, all rejection reasons, URL generation, geo bounds |
| `tests/test_document_builder.py` | Vector document construction | Bilingual output, missing fields, outlier flag, Arabic title/signals, rich feature inclusion |
| `tests/test_vector_store.py` | Dedup key logic | Identical specs, neighbourhood differentiation, area binning |

### Critical Matcher Test Scenarios

- **Near-identical properties match**: ~15m apart, same specs → `confidence > 0.8`, `method="scored_match"`
- **Different rooms → reject**: Same location but different room count → `no_match`
- **Far away despite same specs → reject**: ~21km apart → `no_match` (hard distance ceiling)
- **Area too different → reject**: 100m² vs 200m² → `no_match`
- **No coordinates → attribute fallback**: Matches on type/rooms/area/price when lat/lng missing
- **Building code conflict → hard reject**: B11 vs B6/B8 → instant veto
- **Building code match → accept**: B8 matches existing B8 signal → pass
- **Floor type conflict → hard reject**: Roof vs ground+garden → instant veto
- **Phase conflict → hard reject**: Phase 5 vs Phase 7 → instant veto
- **View conflict (chalets) → hard reject**: Sea view vs lagoon view → instant veto
- **Compound centroid without title evidence → reject**: Same GPS pin, no building/phase → reject
- **Dynamic area tolerance**: Tighter with coords (2% for >200m²), lenient without (5%)

---

## 16. Diagnostic Scripts

### `scripts/check_db.py`

Container healthcheck validating both Supabase connections:
- Runtime connection (transaction pooler, port 6543)
- Migration connection (session/direct, port 5432)
- Executes `SELECT version()` on each; returns exit code 0 if both succeed.

### `scripts/diagnose_grouping.py`

Diagnoses entity resolution candidate grouping quality:
- Counts distinct `property_type` values (warns if >30 → unnormalized strings)
- Counts distinct `rooms` values (warns if >30 → dirty data)
- Counts distinct `(city, neighbourhood)` pairs
- Analyzes group key distribution: singleton %, top 10 largest groups, average group size

### `scripts/print_config.py`

Environment configuration diagnostic:
- Verifies `.env` file exists
- Detects UTF-8 BOM encoding issues (causes silent parsing failures)
- Prints active environment, redacted DB URLs
- Warns if DATABASE_URL points to localhost instead of Supabase

---

## 17. Known Constraints & Future Considerations

### pgvector Index Limitation

The current embedding model (`nvidia/nemotron-3-embed-1b`) produces 2048-dimensional vectors. PostgreSQL pgvector HNSW/IVFFlat indexes are capped at 2000 dimensions, so the system uses **exact cosine scan** instead. This is fast for ~16–50k rows but will need attention at ~100k+ rows. Solution: switch to a ≤1024-dim embedding model and add an HNSW index.

### Entity Resolution Trade-offs

- **False negative preference**: The system deliberately errs on the side of creating new property records rather than risking corruption of existing property clusters.
- **Rule-based vs ML**: Chose deterministic weighted scoring over ML for 100% explainability, zero cold-start dependency, and instant debuggability.
- **`MAX_LISTINGS_PER_PROPERTY = 8`**: Operational cap on listings per property index bucket.

### Stale Listing Detection

Listings are marked `"stale"` if they disappear from Silver in a subsequent run. However, there is no current mechanism to mark them "active" again if they reappear.

### Analysis Matcher vs Entity Matcher

Two separate matching systems exist with different precision/recall trade-offs. The analysis matcher is simpler (exact key match) and covers dashboard use cases where over-grouping is acceptable.

---

## 18. File Map

```
project/
├── data_pipeline.py            # Main 6-stage pipeline CLI entry point
├── scraper.py                  # Algolia search API scraper
├── build_vectors.py            # Vector embedding CLI entry point (opt-in)
│
├── src/
│   ├── __init__.py
│   ├── config.py               # pydantic-settings configuration (all env vars)
│   ├── db.py                   # SQLAlchemy models (10 tables), engine setup
│   ├── logging_config.py       # Centralized logging (text/JSON modes)
│   ├── monitoring.py           # Pipeline run tracking, stage telemetry
│   ├── retry.py                # @with_db_retry decorator (tenacity)
│   │
│   ├── bronze/
│   │   ├── __init__.py
│   │   └── ingest.py           # Raw JSONL → bronze_listings (append-only)
│   │
│   ├── silver/
│   │   ├── __init__.py
│   │   └── transform.py        # Clean, validate, deduplicate → silver_listings
│   │
│   ├── gold/
│   │   ├── __init__.py
│   │   ├── matcher.py          # Entity resolution engine (scoring + hard vetoes)
│   │   ├── build.py            # Gold orchestrator (resolve, aggregate, stale)
│   │   ├── analysis_matcher.py # Dashboard analytics grouping
│   │   └── audit_duplicates.py # CLI dedup audit & CSV export tool
│   │
│   ├── feature_store/
│   │   ├── __init__.py
│   │   └── build.py            # ML feature computation → property_features
│   │
│   └── vector/
│       ├── __init__.py
│       ├── document_builder.py # Bilingual text document construction
│       ├── embedder.py         # NVIDIA NIM embedding API client
│       └── store.py            # pgvector storage + incremental embedding
│
├── alembic/
│   ├── env.py                  # Alembic environment (dynamic URL injection)
│   └── versions/
│       ├── 0001_initial_schema.py      # Full schema (12 tables + pgvector)
│       └── 0002_bronze_idempotency.py  # run_id column + unique constraint
│
├── scripts/
│   ├── check_db.py             # DB connectivity healthcheck
│   ├── diagnose_grouping.py    # Entity resolution diagnostics
│   └── print_config.py         # Environment config diagnostic
│
├── tests/
│   ├── __init__.py
│   ├── test_matcher.py         # 20+ entity resolution test cases
│   ├── test_silver_transform.py # Silver validation & cleaning tests
│   ├── test_document_builder.py # Vector document construction tests
│   └── test_vector_store.py    # Dedup key logic tests
│
├── raw_data/                   # Scraped JSONL files (gitignored)
│   └── _ingested/              # Archive of processed JSONL files
│
├── Dockerfile                  # Multi-stage Python 3.12-slim build
├── docker-compose.yml          # migrate + pipeline + build-vectors services
├── docker-compose.dev.yml      # Local PostgreSQL 16 + pgvector
├── Makefile                    # Developer workflow shortcuts
├── requirements.txt            # Production dependencies (9 packages)
├── requirements-dev.txt        # + pytest + ruff
├── pyproject.toml              # ruff config (line-length=120, py312)
├── alembic.ini                 # Alembic config (URL injected dynamically)
├── .env.example                # Environment variable template
├── .env                        # Active environment config (gitignored)
├── .gitignore
├── .dockerignore
├── ENTITY_RESOLUTION_REDESIGN.md # Architecture decision record
└── PROJECT_DOCUMENTATION.md    # ← THIS FILE
```

---

> **End of document.** This reference covers every table, column, algorithm, threshold, configuration variable, and infrastructure component in the Estatio-AI real estate data pipeline as of August 2026.

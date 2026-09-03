# Egypt Real Estate Price Predictor — Production Package

Split from the original notebook into a deployable package: Supabase data
loading, a training pipeline, and a FastAPI prediction service.

## Layout

```
src/price_predictor/
  config.py                 # every env var / tunable in one place
  data/supabase_loader.py   # pulls the raw listings snapshot from Supabase
  pipeline/cleaning.py      # clean_data() — identical to the notebook
  pipeline/features.py      # engineer_features() + neighbourhood stats lookup
  pipeline/train.py         # model comparison + retrain() -> saves artifact
  pipeline/predict.py       # loads artifact, serves predictions
  api/main.py                # FastAPI app: /predict, /health, /admin/reload
  api/schemas.py             # request/response models
scripts/
  retrain.py                 # CLI entrypoint — manual today, cron later
  run_api.py                 # local dev server
tests/test_pipeline.py       # smoke tests with synthetic data, no creds needed
models/                       # price_model_pipeline.joblib lives here (gitignored)
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .              # installs price_predictor as an importable package
cp .env.example .env          # fill in SUPABASE_URL / SUPABASE_SERVICE_KEY
```

### Supabase table

`SUPABASE_LISTINGS_TABLE` (default `egypt_listings_raw`) must expose these
14 columns — same as `egypt_listings_raw.csv`:

```
property_id, price_egp, price_per_m2, computed_at, days_on_market,
property_type, city, neighbourhood, rooms, baths, area_m2,
active_listing_count, neighbourhood_avg_price_per_m2, is_outlier
```

Use the **service role key** (`SUPABASE_SERVICE_KEY`), not the anon key —
this only ever runs server-side (retrain script / scheduled job), never in
a browser.

If your actual table/view has a different name, just set
`SUPABASE_LISTINGS_TABLE` — no code changes needed.

## Train a model

```bash
# from Supabase
python scripts/retrain.py

# or from a local CSV (e.g. to reproduce the notebook's original run)
python scripts/retrain.py --csv egypt_listings_raw.csv
```

This writes `models/price_model_pipeline.joblib` (used by the API) plus a
timestamped copy in `models/archive/` for rollback. The artifact bundles:
the fitted sklearn pipeline, the model name picked, a neighbourhood
market-stats lookup table, and holdout metrics (MAE/RMSE/R²/MAPE in EGP).

Refuses to save if fewer than `MIN_TRAINING_ROWS` (default 1000) clean rows
come back — a guard against silently deploying a model trained on a broken
or empty Supabase pull.

## Run the API

```bash
python scripts/run_api.py
# or in production:
uvicorn price_predictor.api.main:app --host 0.0.0.0 --port 8000 --workers 2
```

### `POST /predict`

This is what your frontend wizard calls — it maps directly onto the
Location + Property Details steps in your screenshots:

```json
{
  "property_type": "Apartment",
  "city": "Cairo",
  "neighbourhood": "Qattamiya",
  "area_m2": 150,
  "rooms": 3,
  "baths": 2
}
```

```json
{
  "predicted_price_egp": 3079487.38,
  "model_name": "LightGBM",
  "trained_at": "2026-09-02T21:34:07Z",
  "market_context": {
    "neighbourhood_avg_price_per_m2": 24240.09,
    "estimated_active_listings_in_area": 10.0
  }
}
```

**Why the frontend doesn't send `neighbourhood_avg_price_per_m2` or
`active_listing_count`:** those describe market conditions, not the
property itself, and a user filling out a form has no way to know them.
The retrain job precomputes a per-neighbourhood lookup table and bundles
it into the model artifact; the API fills these in server-side. Unseen
neighbourhoods fall back to the city average, then the citywide average
across all data.

Other endpoints: `GET /health` (readiness + model metadata), `POST
/admin/reload` (hot-reload the artifact after a retrain, no restart).

If `PREDICTOR_API_KEY` is set, `/predict` and `/admin/reload` require header
`X-API-Key: <value>`.

## Going from manual to scheduled (every 2 weeks)

Nothing in the code changes — point any scheduler at:

```bash
python scripts/retrain.py --reload-api
```

`--reload-api` calls `POST /admin/reload` on the running API after a
successful retrain so it picks up the new model immediately. Options,
roughly in order of least to most infra:
- **Supabase cron / pg_cron + Edge Function** that shells out to this command (or calls a small wrapper service)
- **GitHub Actions** scheduled workflow (`cron: '0 3 */14 * *'`) running against a self-hosted or cloud runner with network access to Supabase + the API
- **A systemd timer / cron job** on the same box as the API
- **Airflow / Prefect** if you already run one for other pipelines

## Docker

```bash
docker build -t price-predictor .
docker run -p 8000:8000 --env-file .env -v $(pwd)/models:/app/models price-predictor
```

## Tests

```bash
pytest -q
```

Uses synthetic data — runs without Supabase credentials, safe for CI.

## Notes carried over from the notebook (Section 9)

- Once you have multiple Supabase snapshots (after a few 2-week cycles),
  switch the holdout split in `train.py` from random to time-based (train
  on older snapshots, test on the newest) — a random split will
  overestimate real-world accuracy once there's a time dimension. `config.py`
  has `TEST_SIZE`; the split itself is in `retrain()`.
- `computed_at` / `days_on_market` are currently dropped as leakage/useless
  (constant in a single snapshot). Once snapshots accumulate, consider
  re-adding `days_on_market` as a real feature and using `computed_at` to
  dedupe to the latest snapshot per `property_id` before training.
- `retrain()` re-runs the full model comparison every time by default
  (slower but keeps picking the best model as data evolves). Pass
  `--model <name>` to `scripts/retrain.py` to skip that and force a specific
  model once you're confident which one wins consistently.

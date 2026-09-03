"""
Central configuration for the Egypt Real Estate Price Predictor.

Everything that might need to change between environments (dev/staging/prod)
or that multiple modules need to agree on (feature lists, model choices)
lives here so there's a single source of truth.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[2]  # project root
load_dotenv(BASE_DIR / ".env")
MODELS_DIR = Path(os.getenv("MODELS_DIR", BASE_DIR / "models"))
MODELS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_ARTIFACT_PATH = MODELS_DIR / "price_model_pipeline.joblib"
# Every retrain also writes a timestamped copy here so you can roll back
# without needing to have kept your own backups.
MODEL_ARCHIVE_DIR = MODELS_DIR / "archive"
MODEL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")  # use the service role key server-side, never the anon key
# Name of the table/view holding the raw listings snapshot (same 14 columns
# as egypt_listings_raw.csv). Change via env var if your table is named
# differently, e.g. "gold_listings" or a view.
SUPABASE_LISTINGS_TABLE = os.getenv("SUPABASE_LISTINGS_TABLE", "egypt_listings_raw")
# How many rows to pull per page when reading from Supabase (PostgREST caps
# a single request's rows; we page through in batches).
SUPABASE_PAGE_SIZE = int(os.getenv("SUPABASE_PAGE_SIZE", "1000"))

# ---------------------------------------------------------------------------
# Raw columns expected from Supabase (mirrors egypt_listings_raw.csv)
# ---------------------------------------------------------------------------
RAW_COLUMNS = [
    "property_id",
    "price_egp",
    "price_per_m2",
    "computed_at",
    "days_on_market",
    "property_type",
    "city",
    "neighbourhood",
    "rooms",
    "baths",
    "area_m2",
    "active_listing_count",
    "neighbourhood_avg_price_per_m2",
    "is_outlier",
]

# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
LEAKY_OR_USELESS_COLS = ["property_id", "price_per_m2", "computed_at", "days_on_market"]
OUTLIER_LOWER_Q = 0.005
OUTLIER_UPPER_Q = 0.995
OUTLIER_SEGMENT_COLS = ("price_per_m2", "area_m2")

# ---------------------------------------------------------------------------
# Feature engineering / modeling
# ---------------------------------------------------------------------------
NUMERIC_FEATURES = [
    "rooms", "baths", "area_m2", "area_per_room", "bath_to_room_ratio",
    "active_listing_count_log", "neighbourhood_avg_price_per_m2", "neighbourhood_is_known",
]
CATEGORICAL_ONEHOT = ["property_type"]
CATEGORICAL_TARGET_ENC = ["city", "neighbourhood"]
TARGET_COL = "log_price"
FEATURE_COLS = NUMERIC_FEATURES + CATEGORICAL_ONEHOT + CATEGORICAL_TARGET_ENC

RANDOM_STATE = 42
TEST_SIZE = 0.2
CV_FOLDS = 5

# Minimum number of rows required before we'll accept a retrain result.
# Guards against retraining on a broken/empty Supabase pull and silently
# overwriting a good production model.
MIN_TRAINING_ROWS = int(os.getenv("MIN_TRAINING_ROWS", "1000"))

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_KEY = os.getenv("PREDICTOR_API_KEY", "")  # if set, required via X-API-Key header
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")

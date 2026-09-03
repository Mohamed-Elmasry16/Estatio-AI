"""
Smoke tests for the cleaning -> features -> train -> predict pipeline, using
synthetic data so they run without Supabase credentials or the real CSV.

Run with:  pytest -q
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from price_predictor.pipeline.cleaning import clean_data
from price_predictor.pipeline.features import build_neighbourhood_stats, city_fallback_stats, engineer_features, prepare_inference_features
from price_predictor.pipeline.train import retrain


def make_synthetic_raw(n=2000, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    property_types = rng.choice(["Apartment", "Villa", "Land"], size=n, p=[0.7, 0.2, 0.1])
    cities = rng.choice(["Cairo", "Giza", "Alexandria"], size=n)
    neighbourhoods = rng.choice(["Maadi", "Nasr City", "Zamalek", "Sheikh Zayed"], size=n)
    area = rng.uniform(60, 400, size=n)
    rooms = rng.integers(1, 6, size=n).astype(float)
    baths = rng.integers(1, 4, size=n).astype(float)
    price_per_m2 = rng.uniform(8000, 40000, size=n)
    price = price_per_m2 * area

    df = pd.DataFrame({
        "property_id": range(n),
        "price_egp": price,
        "price_per_m2": price_per_m2,
        "computed_at": "2026-01-01",
        "days_on_market": 0,
        "property_type": property_types,
        "city": cities,
        "neighbourhood": neighbourhoods,
        "rooms": rooms,
        "baths": baths,
        "area_m2": area,
        "active_listing_count": rng.integers(1, 20, size=n),
        "neighbourhood_avg_price_per_m2": price_per_m2 * rng.uniform(0.9, 1.1, size=n),
        "is_outlier": False,
    })
    return df


def test_clean_data_drops_leakage_cols():
    raw = make_synthetic_raw()
    cleaned = clean_data(raw)
    for col in ["property_id", "price_per_m2", "computed_at", "days_on_market"]:
        assert col not in cleaned.columns
    assert len(cleaned) > 0


def test_engineer_features_shapes():
    raw = make_synthetic_raw()
    cleaned = clean_data(raw)
    feat = engineer_features(cleaned)
    assert "log_price" in feat.columns
    assert feat["neighbourhood_is_known"].isin([0, 1]).all()


def test_full_retrain_and_predict(tmp_path):
    raw = make_synthetic_raw(n=3000)
    save_path = tmp_path / "model.joblib"

    artifact = retrain(raw, model_name="Ridge", save_path=save_path, do_holdout_eval=True)

    assert save_path.exists()
    assert artifact["holdout_metrics"]["r2"] > 0.3  # synthetic data is easy; sanity floor only

    from price_predictor.pipeline.predict import PricePredictor
    predictor = PricePredictor(artifact_path=save_path)
    predictor.load()

    result = predictor.predict_one(
        property_type="Apartment", city="Cairo", neighbourhood="Maadi",
        area_m2=150, rooms=3, baths=2,
    )
    assert result["predicted_price_egp"] > 0


def test_predict_unseen_neighbourhood_falls_back():
    raw = make_synthetic_raw()
    cleaned = clean_data(raw)
    stats = build_neighbourhood_stats(cleaned)
    fallback = city_fallback_stats(stats)

    unseen = pd.DataFrame([{
        "property_type": "Apartment", "city": "Cairo", "neighbourhood": "Some New Compound",
        "area_m2": 150, "rooms": 3, "baths": 2,
    }])
    feat = prepare_inference_features(
        unseen, stats, fallback,
        float(stats["neighbourhood_avg_price_per_m2"].mean()),
        float(stats["active_listing_count"].mean()),
    )
    assert not feat["neighbourhood_avg_price_per_m2"].isna().any()

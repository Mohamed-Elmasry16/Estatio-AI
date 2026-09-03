"""
Feature engineering — same logic as the notebook's Section 4, plus one
addition needed for production: a neighbourhood stats lookup table.

Why the lookup table exists
----------------------------
Two of the model's features — `neighbourhood_avg_price_per_m2` and
`active_listing_count` — describe market conditions, not the property being
priced. A user filling out the frontend wizard (governorate/city/neighbourhood,
property type, area, rooms, baths) has no way to supply them.

So at retrain time we compute, per (city, neighbourhood), the average
price_per_m2 and the average active_listing_count from the training data,
and bundle that lookup table into the saved model artifact. At inference
time, `prepare_inference_features()` looks the values up by the city/
neighbourhood the user picked instead of requiring them as inputs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Same function as the notebook's engineer_features(), unchanged."""
    df = df.copy()

    df["rooms"] = df.groupby("property_type")["rooms"].transform(lambda s: s.fillna(s.median()))
    df["baths"] = df.groupby("property_type")["baths"].transform(lambda s: s.fillna(s.median()))
    # fallback in case a property_type has ALL rooms/baths missing (median would still be NaN)
    df["rooms"] = df["rooms"].fillna(df["rooms"].median())
    df["baths"] = df["baths"].fillna(df["baths"].median())

    df["neighbourhood_is_known"] = df["neighbourhood"].notna().astype(int)
    df["neighbourhood"] = df["neighbourhood"].fillna("Unknown")

    df["area_per_room"] = df["area_m2"] / df["rooms"].replace(0, np.nan)
    df["area_per_room"] = df["area_per_room"].fillna(df["area_m2"])  # studios / land (0 rooms)
    df["bath_to_room_ratio"] = df["baths"] / df["rooms"].replace(0, np.nan)
    df["bath_to_room_ratio"] = df["bath_to_room_ratio"].fillna(0)

    df["active_listing_count_log"] = np.log1p(df["active_listing_count"])

    df["log_price"] = np.log1p(df["price_egp"])

    return df


def build_neighbourhood_stats(cleaned_df: pd.DataFrame) -> pd.DataFrame:
    """Compute per (city, neighbourhood) market stats from cleaned training
    data, to be bundled with the model and used at inference time.

    `clean_data()` already drops `price_per_m2` (it's leakage as a *feature*,
    but it's exactly what we want to aggregate here), so it's recomputed
    on the fly from price_egp / area_m2 rather than requiring callers to
    pass the pre-drop frame.

    Returns columns: city, neighbourhood, neighbourhood_avg_price_per_m2,
    active_listing_count.
    """
    df = cleaned_df.copy()
    if "price_per_m2" not in df.columns:
        df["price_per_m2"] = df["price_egp"] / df["area_m2"].replace(0, np.nan)

    stats = (
        df.groupby(["city", "neighbourhood"], dropna=False)
        .agg(
            neighbourhood_avg_price_per_m2=("price_per_m2", "mean"),
            active_listing_count=("active_listing_count", "mean"),
        )
        .reset_index()
    )
    return stats


# Global fallback used when a (city, neighbourhood) pair wasn't seen in
# training data (new area, typo, etc.) — the citywide average, and if even
# that's missing, the overall average across all training data.
def city_fallback_stats(neighbourhood_stats: pd.DataFrame) -> pd.DataFrame:
    return (
        neighbourhood_stats.groupby("city", dropna=False)
        .agg(
            neighbourhood_avg_price_per_m2=("neighbourhood_avg_price_per_m2", "mean"),
            active_listing_count=("active_listing_count", "mean"),
        )
        .reset_index()
    )


def prepare_inference_features(
    raw_input: pd.DataFrame,
    neighbourhood_stats: pd.DataFrame,
    fallback_stats: pd.DataFrame,
    global_avg_price_per_m2: float,
    global_avg_listing_count: float,
) -> pd.DataFrame:
    """
    Turn raw user-submitted rows (property_type, city, neighbourhood, area_m2,
    rooms, baths — exactly what the frontend wizard collects) into the feature
    frame the model expects, filling in market-condition features from the
    bundled lookup table instead of requiring the caller to supply them.

    Falls back city-wide, then globally, if the exact neighbourhood (or city)
    wasn't present in the training data.
    """
    df = raw_input.copy()

    merged = df.merge(neighbourhood_stats, on=["city", "neighbourhood"], how="left")

    missing = merged["neighbourhood_avg_price_per_m2"].isna()
    if missing.any():
        city_merged = merged.loc[missing, ["city"]].merge(fallback_stats, on="city", how="left")
        merged.loc[missing, "neighbourhood_avg_price_per_m2"] = city_merged["neighbourhood_avg_price_per_m2"].values
        merged.loc[missing, "active_listing_count"] = city_merged["active_listing_count"].values

    merged["neighbourhood_avg_price_per_m2"] = merged["neighbourhood_avg_price_per_m2"].fillna(global_avg_price_per_m2)
    merged["active_listing_count"] = merged["active_listing_count"].fillna(global_avg_listing_count)

    # Reuse the same feature logic as training, minus the log_price target
    # (unknown at inference time) and minus dropping is_outlier (not present).
    merged["rooms"] = merged["rooms"].fillna(merged["rooms"].median())
    merged["baths"] = merged["baths"].fillna(merged["baths"].median())

    merged["neighbourhood_is_known"] = merged["neighbourhood"].notna().astype(int)
    merged["neighbourhood"] = merged["neighbourhood"].fillna("Unknown")

    merged["area_per_room"] = merged["area_m2"] / merged["rooms"].replace(0, np.nan)
    merged["area_per_room"] = merged["area_per_room"].fillna(merged["area_m2"])
    merged["bath_to_room_ratio"] = merged["baths"] / merged["rooms"].replace(0, np.nan)
    merged["bath_to_room_ratio"] = merged["bath_to_room_ratio"].fillna(0)

    merged["active_listing_count_log"] = np.log1p(merged["active_listing_count"])

    return merged

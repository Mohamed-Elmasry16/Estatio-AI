"""
Model training — same model comparison and retrain() logic as the notebook's
Sections 6-8, extracted so a scheduler can call it unattended.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import KFold, cross_validate, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler, TargetEncoder

import lightgbm as lgb

from price_predictor import config
from price_predictor.pipeline.cleaning import clean_data
from price_predictor.pipeline.features import build_neighbourhood_stats, engineer_features

logger = logging.getLogger(__name__)


def build_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), config.NUMERIC_FEATURES),
            ("onehot", OneHotEncoder(handle_unknown="ignore"), config.CATEGORICAL_ONEHOT),
            ("target_enc", TargetEncoder(random_state=config.RANDOM_STATE), config.CATEGORICAL_TARGET_ENC),
        ],
        remainder="drop",
    )


def build_model_registry() -> dict:
    """Same four candidates as the notebook. Kept as a function (not a
    module-level constant) so each call gets fresh, unfitted estimators."""
    return {
        "Dummy (median)": DummyRegressor(strategy="median"),
        "Ridge": Ridge(alpha=1.0, random_state=config.RANDOM_STATE),
        "RandomForest": RandomForestRegressor(
            n_estimators=300, max_depth=None, min_samples_leaf=2,
            n_jobs=-1, random_state=config.RANDOM_STATE,
        ),
        "HistGradientBoosting": HistGradientBoostingRegressor(random_state=config.RANDOM_STATE),
        "LightGBM": lgb.LGBMRegressor(
            n_estimators=400, learning_rate=0.05, num_leaves=63,
            random_state=config.RANDOM_STATE, verbosity=-1,
        ),
    }


def compare_models(X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
    """5-fold CV comparison across all candidates. Returns a results table
    sorted best (highest R2) first."""
    preprocessor = build_preprocessor()
    models = build_model_registry()
    cv = KFold(n_splits=config.CV_FOLDS, shuffle=True, random_state=config.RANDOM_STATE)

    results = []
    for name, model in models.items():
        pipe = Pipeline([("prep", preprocessor), ("model", model)])
        scores = cross_validate(
            pipe, X, y, cv=cv,
            scoring={"mae": "neg_mean_absolute_error", "rmse": "neg_root_mean_squared_error", "r2": "r2"},
            n_jobs=-1,
        )
        results.append({
            "model": name,
            "MAE (log scale)": -scores["test_mae"].mean(),
            "RMSE (log scale)": -scores["test_rmse"].mean(),
            "R2": scores["test_r2"].mean(),
        })
        logger.info("%-22s CV done", name)

    return pd.DataFrame(results).sort_values("R2", ascending=False).reset_index(drop=True)


def evaluate_on_holdout(pipe: Pipeline, X_test: pd.DataFrame, y_test: pd.Series) -> dict:
    """Errors reported back on the original EGP scale, matching the notebook."""
    y_pred_log = pipe.predict(X_test)
    y_pred = np.expm1(y_pred_log)
    y_true = np.expm1(y_test)

    mae = mean_absolute_error(y_true, y_pred)
    rmse = mean_squared_error(y_true, y_pred) ** 0.5
    r2 = r2_score(y_true, y_pred)
    mape = float(np.mean(np.abs((y_true - y_pred) / y_true)) * 100)

    return {"mae_egp": mae, "rmse_egp": rmse, "r2": r2, "mape_pct": mape}


def retrain(
    raw_df: pd.DataFrame,
    model_name: str | None = None,
    save_path=None,
    do_holdout_eval: bool = True,
) -> dict:
    """
    Full retrain from a raw listings DataFrame (as returned by
    `supabase_loader.load_raw_listings()`) to a saved, ready-to-serve
    artifact.

    Pass model_name to force a specific model; otherwise re-runs the CV
    comparison and picks the best one automatically (slower, but keeps
    picking the best model as the data distribution evolves over time).

    Returns a dict with the fitted pipeline, chosen model name, holdout
    metrics (if computed), and the path the artifact was saved to. Raises
    ValueError if there isn't enough clean data to train on (guards against
    silently deploying a model trained on a broken data pull).
    """
    save_path = save_path or config.MODEL_ARTIFACT_PATH

    cleaned = clean_data(raw_df)
    if len(cleaned) < config.MIN_TRAINING_ROWS:
        raise ValueError(
            f"Only {len(cleaned)} clean rows after filtering (minimum is "
            f"{config.MIN_TRAINING_ROWS}). Refusing to retrain on this data — "
            "check the Supabase pull before retrying."
        )

    neighbourhood_stats = build_neighbourhood_stats(cleaned)
    from price_predictor.pipeline.features import city_fallback_stats
    fallback_stats = city_fallback_stats(neighbourhood_stats)
    global_avg_price_per_m2 = float(neighbourhood_stats["neighbourhood_avg_price_per_m2"].mean())
    global_avg_listing_count = float(neighbourhood_stats["active_listing_count"].mean())

    feat = engineer_features(cleaned)
    X = feat[config.FEATURE_COLS]
    y = feat[config.TARGET_COL]

    results_df = None
    if model_name is None:
        results_df = compare_models(X, y)
        model_name = results_df.iloc[0]["model"]
        logger.info("Best model by CV R2: %s", model_name)

    preprocessor = build_preprocessor()
    models = build_model_registry()
    final_pipe = Pipeline([("prep", preprocessor), ("model", models[model_name])])

    holdout_metrics = None
    if do_holdout_eval:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=config.TEST_SIZE, random_state=config.RANDOM_STATE
        )
        final_pipe.fit(X_train, y_train)
        holdout_metrics = evaluate_on_holdout(final_pipe, X_test, y_test)
        logger.info("Holdout metrics: %s", holdout_metrics)
        # Refit on the full dataset for the artifact that actually gets served
        final_pipe.fit(X, y)
    else:
        final_pipe.fit(X, y)

    artifact = {
        "pipeline": final_pipe,
        "model_name": model_name,
        "feature_cols": config.FEATURE_COLS,
        "neighbourhood_stats": neighbourhood_stats,
        "fallback_stats": fallback_stats,
        "global_avg_price_per_m2": global_avg_price_per_m2,
        "global_avg_listing_count": global_avg_listing_count,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_training_rows": len(feat),
        "holdout_metrics": holdout_metrics,
        "cv_comparison": results_df.to_dict(orient="records") if results_df is not None else None,
    }

    joblib.dump(artifact, save_path)

    # Timestamped archive copy for rollback
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = config.MODEL_ARCHIVE_DIR / f"price_model_pipeline_{ts}.joblib"
    joblib.dump(artifact, archive_path)

    logger.info("Retrained with %s on %d rows -> saved to %s (archived at %s)", model_name, len(feat), save_path, archive_path)
    return artifact

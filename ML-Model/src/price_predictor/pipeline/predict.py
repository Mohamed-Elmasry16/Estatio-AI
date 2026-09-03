"""
Inference — loads the artifact saved by train.retrain() and exposes a
PricePredictor class used by both the API and any batch/offline scoring.
"""
from __future__ import annotations

import logging
import threading

import joblib
import numpy as np
import pandas as pd

from price_predictor import config
from price_predictor.pipeline.features import prepare_inference_features

logger = logging.getLogger(__name__)


class ModelNotLoadedError(RuntimeError):
    pass


class PricePredictor:
    """
    Thread-safe wrapper around the fitted pipeline + neighbourhood stats
    lookup. One instance is created at API startup and reused across
    requests; call `reload()` after a retrain to pick up the new artifact
    without restarting the process.
    """

    def __init__(self, artifact_path=None):
        self.artifact_path = artifact_path or config.MODEL_ARTIFACT_PATH
        self._lock = threading.RLock()
        self._pipeline = None
        self._feature_cols = None
        self._neighbourhood_stats = None
        self._fallback_stats = None
        self._global_avg_price_per_m2 = None
        self._global_avg_listing_count = None
        self._meta = {}

    def load(self) -> None:
        with self._lock:
            artifact = joblib.load(self.artifact_path)
            self._pipeline = artifact["pipeline"]
            self._feature_cols = artifact["feature_cols"]
            self._neighbourhood_stats = artifact["neighbourhood_stats"]
            self._fallback_stats = artifact["fallback_stats"]
            self._global_avg_price_per_m2 = artifact["global_avg_price_per_m2"]
            self._global_avg_listing_count = artifact["global_avg_listing_count"]
            self._meta = {
                "model_name": artifact.get("model_name"),
                "trained_at": artifact.get("trained_at"),
                "n_training_rows": artifact.get("n_training_rows"),
                "holdout_metrics": artifact.get("holdout_metrics"),
            }
            logger.info("Loaded model artifact from %s (%s)", self.artifact_path, self._meta)

    def reload(self) -> None:
        self.load()

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    @property
    def metadata(self) -> dict:
        return dict(self._meta)

    def predict_one(
        self,
        property_type: str,
        city: str,
        neighbourhood: str,
        area_m2: float,
        rooms: float,
        baths: float,
    ) -> dict:
        """Predict a single property's price from the fields a frontend
        wizard collects. Returns the point estimate plus the metadata used
        to derive market-condition features, for transparency/debugging."""
        with self._lock:
            if not self.is_loaded:
                raise ModelNotLoadedError("Model artifact not loaded — call load() first.")

            raw_input = pd.DataFrame([{
                "property_type": property_type,
                "city": city,
                "neighbourhood": neighbourhood,
                "area_m2": area_m2,
                "rooms": rooms,
                "baths": baths,
            }])

            feat = prepare_inference_features(
                raw_input,
                self._neighbourhood_stats,
                self._fallback_stats,
                self._global_avg_price_per_m2,
                self._global_avg_listing_count,
            )

            X = feat[self._feature_cols]
            log_pred = self._pipeline.predict(X)
            price_egp = float(np.expm1(log_pred)[0])

            used_neighbourhood_avg = float(feat["neighbourhood_avg_price_per_m2"].iloc[0])
            used_active_listings = float(np.expm1(feat["active_listing_count_log"].iloc[0]))

            return {
                "predicted_price_egp": price_egp,
                "model_name": self._meta.get("model_name"),
                "trained_at": self._meta.get("trained_at"),
                "market_context": {
                    "neighbourhood_avg_price_per_m2": used_neighbourhood_avg,
                    "estimated_active_listings_in_area": used_active_listings,
                },
            }

    def predict_batch(self, raw_rows: pd.DataFrame) -> np.ndarray:
        """Predict for many rows already in the raw CSV/Supabase shape
        (used for offline scoring/backtesting, not the live API)."""
        with self._lock:
            if not self.is_loaded:
                raise ModelNotLoadedError("Model artifact not loaded — call load() first.")

            feat = prepare_inference_features(
                raw_rows,
                self._neighbourhood_stats,
                self._fallback_stats,
                self._global_avg_price_per_m2,
                self._global_avg_listing_count,
            )
            X = feat[self._feature_cols]
            log_pred = self._pipeline.predict(X)
            return np.expm1(log_pred)


# Module-level singleton used by the API (simple, process-wide, thread-safe
# via the lock above).
predictor = PricePredictor()

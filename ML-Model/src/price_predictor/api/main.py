"""
Production API for the Egypt Real Estate Price Predictor.

Run locally:
    uvicorn price_predictor.api.main:app --reload --port 8000

Run in production (example):
    uvicorn price_predictor.api.main:app --host 0.0.0.0 --port 8000 --workers 2

The frontend wizard (Location -> Property Details -> Get Estimate) maps
directly onto POST /predict: property_type, city, neighbourhood, area_m2,
rooms, baths. Everything else the model needs (neighbourhood market stats)
is filled in server-side from the bundled lookup table.
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from price_predictor import config
from price_predictor.api.schemas import HealthResponse, PredictRequest, PredictResponse
from price_predictor.pipeline.predict import ModelNotLoadedError, predictor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Egypt Real Estate Price Predictor API",
    version="1.0.0",
    description="Predicts an estimated market price (EGP) for a property from its location and details.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _load_model_on_startup() -> None:
    try:
        predictor.load()
    except FileNotFoundError:
        # Don't crash the process if no model has been trained yet — /health
        # will report model_loaded=False and /predict will 503 until a
        # retrain runs and calls /reload (or the process is restarted).
        logger.warning("No model artifact found at %s yet. /predict will 503 until one is trained.", config.MODEL_ARTIFACT_PATH)


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """No-op if PREDICTOR_API_KEY isn't set (e.g. local dev). Set it in
    production and have the frontend/backend send X-API-Key."""
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    meta = predictor.metadata
    return HealthResponse(
        status="ok",
        model_loaded=predictor.is_loaded,
        model_name=meta.get("model_name"),
        trained_at=meta.get("trained_at"),
        n_training_rows=meta.get("n_training_rows"),
    )


@app.post("/predict", response_model=PredictResponse, dependencies=[Depends(verify_api_key)])
def predict(payload: PredictRequest) -> PredictResponse:
    try:
        result = predictor.predict_one(
            property_type=payload.property_type,
            city=payload.city,
            neighbourhood=payload.neighbourhood,
            area_m2=payload.area_m2,
            rooms=payload.rooms,
            baths=payload.baths,
        )
    except ModelNotLoadedError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model not loaded yet. Run a retrain, then call POST /admin/reload.",
        )
    except Exception:
        logger.exception("Prediction failed for payload=%s", payload)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not generate a prediction for the given input.")

    return PredictResponse(**result)


@app.post("/admin/reload", dependencies=[Depends(verify_api_key)])
def reload_model() -> dict:
    """Call this after a retrain finishes so the running API picks up the
    new artifact without a restart. Wire this into the end of your retrain
    job (scripts/retrain.py already does it if API_RELOAD_URL is set)."""
    try:
        predictor.reload()
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No artifact found at {config.MODEL_ARTIFACT_PATH}")
    return {"status": "reloaded", **predictor.metadata}

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PredictRequest(BaseModel):
    property_type: str = Field(..., examples=["Apartment"], description="Must match a category seen in training, e.g. Apartment, Villa, Land — unseen types fall back gracefully but accuracy drops.")
    city: str = Field(..., examples=["Cairo"])
    neighbourhood: str = Field(..., examples=["Qattamiya"])
    area_m2: float = Field(..., gt=0, examples=[150])
    rooms: float = Field(..., ge=0, examples=[3])
    baths: float = Field(..., ge=0, examples=[2])


class MarketContext(BaseModel):
    neighbourhood_avg_price_per_m2: float
    estimated_active_listings_in_area: float


class PredictResponse(BaseModel):
    predicted_price_egp: float
    model_name: Optional[str] = None
    trained_at: Optional[str] = None
    market_context: MarketContext


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_name: Optional[str] = None
    trained_at: Optional[str] = None
    n_training_rows: Optional[int] = None


class ErrorResponse(BaseModel):
    detail: str

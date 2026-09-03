#!/usr/bin/env python3
"""Convenience runner for local dev: python scripts/run_api.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("price_predictor.api.main:app", host="0.0.0.0", port=8000, reload=True)

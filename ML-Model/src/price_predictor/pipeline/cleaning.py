"""
Data cleaning — identical logic to the notebook's Section 3, so training and
any future re-analysis in a notebook never drift out of sync with production.
"""
from __future__ import annotations

import logging

import pandas as pd

from price_predictor import config

logger = logging.getLogger(__name__)


def filter_segment_outliers(
    df: pd.DataFrame,
    cols: tuple[str, ...] = config.OUTLIER_SEGMENT_COLS,
    lower_q: float = config.OUTLIER_LOWER_Q,
    upper_q: float = config.OUTLIER_UPPER_Q,
) -> pd.DataFrame:
    """Drop rows outside the [lower_q, upper_q] percentile range of `cols`,
    computed separately per property_type so land/villas aren't clipped by
    apartment-scale thresholds."""
    keep_mask = pd.Series(True, index=df.index)
    for ptype, group in df.groupby("property_type"):
        for col in cols:
            lo, hi = group[col].quantile([lower_q, upper_q])
            out_of_range = (df.index.isin(group.index)) & ((df[col] < lo) | (df[col] > hi))
            keep_mask &= ~out_of_range
    return df[keep_mask]


def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    """Drop flagged + statistical outliers and leakage/useless columns.
    Same function as the notebook's clean_data(), unchanged."""
    df = df.copy()

    before = len(df)
    df = df[~df["is_outlier"]]
    df = filter_segment_outliers(df)
    df = df[df["price_egp"] > 0]
    after = len(df)
    dropped = before - after
    pct = dropped / before if before else 0.0
    logger.info("Rows dropped by outlier filtering: %d (%.1f%%) -- %d remain", dropped, pct * 100, after)

    df = df.drop(columns=[c for c in config.LEAKY_OR_USELESS_COLS if c in df.columns])
    return df

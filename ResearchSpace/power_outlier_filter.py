#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared power-series outlier filtering for satellite telemetry."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_ROBUST_QUANTILE = 0.99
DEFAULT_ROBUST_MULTIPLIER = 1000.0


def _rounded(value: float | None, digits: int = 9) -> float | None:
    if value is None:
        return None
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def filter_power_values(
    values: pd.Series | np.ndarray | list[float],
    *,
    upper_limit_W: float | None = None,
    robust_quantile: float = DEFAULT_ROBUST_QUANTILE,
    robust_multiplier: float = DEFAULT_ROBUST_MULTIPLIER,
) -> tuple[pd.Series, dict[str, Any]]:
    """Return non-negative power values with implausible extremes set to ``NaN``.

    A configured screening upper limit always takes precedence. Without one,
    the filter removes values above a deliberately wide robust upper tail. The
    configured value may come from a physical rating or a documented modelling
    envelope; either way it is a screening threshold, not a capacity claim.
    """
    if not 0.5 <= robust_quantile < 1.0:
        raise ValueError("robust_quantile must be in [0.5, 1)")
    if (not math.isfinite(float(robust_multiplier))
            or float(robust_multiplier) <= 1.0):
        raise ValueError("robust_multiplier must be finite and greater than 1")

    if isinstance(values, pd.Series):
        numeric = pd.to_numeric(values, errors="coerce").astype(float)
    else:
        numeric = pd.Series(
            pd.to_numeric(pd.Series(values), errors="coerce"),
            dtype=float,
        )
    finite_nonnegative = numeric.notna() & numeric.ge(0)
    raw_values = numeric.loc[finite_nonnegative].to_numpy(dtype=float)

    q99 = (
        float(np.quantile(raw_values, 0.99))
        if len(raw_values) else None
    )
    tail_quantile = (
        float(np.quantile(raw_values, robust_quantile))
        if len(raw_values) else None
    )
    method = "unbounded_no_samples"
    upper_limit: float | None = None
    if upper_limit_W is not None:
        configured = float(upper_limit_W)
        if not math.isfinite(configured) or configured <= 0:
            raise ValueError("upper_limit_W must be finite and positive")
        upper_limit = configured
        method = "configured_max_power"
    elif tail_quantile is not None and tail_quantile > 0:
        upper_limit = max(
            tail_quantile * float(robust_multiplier),
            float(q99 or 0.0) * 10.0,
            np.nextafter(tail_quantile, math.inf),
        )
        method = "robust_extreme_tail"
    elif raw_values.size:
        method = "unbounded_no_positive_tail"

    outlier_mask = (
        finite_nonnegative & numeric.gt(upper_limit)
        if upper_limit is not None
        else pd.Series(False, index=numeric.index, dtype=bool)
    )
    valid_mask = finite_nonnegative & ~outlier_mask
    cleaned = numeric.where(valid_mask)
    metadata = {
        "method": method,
        "upper_limit_W": _rounded(upper_limit),
        "robust_quantile": (
            float(robust_quantile)
            if method == "robust_extreme_tail" else None
        ),
        "robust_multiplier": (
            float(robust_multiplier)
            if method == "robust_extreme_tail" else None
        ),
        "raw_nonnegative_samples": int(finite_nonnegative.sum()),
        "valid_samples": int(valid_mask.sum()),
        "outlier_samples": int(outlier_mask.sum()),
        "invalid_samples": int((~finite_nonnegative).sum()),
        "raw_p99_W": _rounded(q99),
        "raw_tail_W": _rounded(tail_quantile),
        "raw_max_W": (
            _rounded(float(np.max(raw_values)))
            if raw_values.size else None
        ),
    }
    return cleaned, metadata

"""
Calibration fitter — Phase 2.

Fits Platt Scaling (logistic regression on raw_probability) and
Isotonic Regression to correct systematic over/under-confidence.

Guard: n < 30 → returns {ok: False, reason: 'insufficient_samples'}.

Output: calibration_map — 100-point breakpoint list stored in
calibration_runs.payload for runtime lookup via apply_calibration_map().
"""
from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

MIN_SAMPLES = 30
_MAP_POINTS = 100
_GRID = [i / (_MAP_POINTS - 1) * 0.98 + 0.01 for i in range(_MAP_POINTS)]  # 0.01 → 0.99


def _build_map(raw_grid: list[float], calibrated_grid: list[float]) -> list[dict]:
    return [
        {"raw": round(r, 4), "calibrated": round(max(0.001, min(c, 0.999)), 6)}
        for r, c in zip(raw_grid, calibrated_grid, strict=True)
    ]


def fit_platt(raw_probs: list[float], outcomes: list[int]) -> dict:
    n = len(raw_probs)
    if n < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient_samples", "n": n}

    X = np.array(raw_probs).reshape(-1, 1)
    y = np.array(outcomes)
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=500)
    lr.fit(X, y)

    grid_X = np.array(_GRID).reshape(-1, 1)
    calibrated = lr.predict_proba(grid_X)[:, 1].tolist()

    return {
        "ok": True,
        "method": "PLATT",
        "n": n,
        "coef": float(lr.coef_[0][0]),
        "intercept": float(lr.intercept_[0]),
        "calibration_map": _build_map(_GRID, calibrated),
    }


def fit_isotonic(raw_probs: list[float], outcomes: list[int]) -> dict:
    n = len(raw_probs)
    if n < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient_samples", "n": n}

    order = np.argsort(raw_probs)
    X_sorted = np.array(raw_probs)[order]
    y_sorted = np.array(outcomes)[order]

    ir = IsotonicRegression(out_of_bounds="clip")
    ir.fit(X_sorted, y_sorted)

    calibrated = ir.predict(_GRID).tolist()

    return {
        "ok": True,
        "method": "ISOTONIC",
        "n": n,
        "calibration_map": _build_map(_GRID, calibrated),
    }


def apply_calibration_map(raw_prob: float, calibration_map: list[dict]) -> float:
    """Linear interpolation over the calibration breakpoints."""
    if not calibration_map:
        return raw_prob

    raws = [p["raw"] for p in calibration_map]
    cals = [p["calibrated"] for p in calibration_map]

    if raw_prob <= raws[0]:
        return float(cals[0])
    if raw_prob >= raws[-1]:
        return float(cals[-1])

    for i in range(len(raws) - 1):
        if raws[i] <= raw_prob <= raws[i + 1]:
            t = (raw_prob - raws[i]) / (raws[i + 1] - raws[i])
            interpolated = cals[i] + t * (cals[i + 1] - cals[i])
            return round(max(0.001, min(interpolated, 0.999)), 6)

    return raw_prob

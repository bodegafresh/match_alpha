from __future__ import annotations

from dataclasses import dataclass
from math import log


@dataclass(frozen=True)
class CalibrationMetrics:
    brier_score: float
    log_loss: float
    ece: float
    sharpness: float
    sample_size: int


def calibration_metrics(predictions: list[float], outcomes: list[int], bins: int = 10) -> CalibrationMetrics:
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must have same length")
    if not predictions:
        return CalibrationMetrics(0.0, 0.0, 0.0, 0.0, 0)
    eps = 1e-12
    n = len(predictions)
    brier = sum((p - y) ** 2 for p, y in zip(predictions, outcomes, strict=True)) / n
    loss = -sum(y * log(max(p, eps)) + (1 - y) * log(max(1 - p, eps)) for p, y in zip(predictions, outcomes, strict=True)) / n
    sharpness = sum(abs(p - 0.5) for p in predictions) / n
    ece = 0.0
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        bucket = [(p, y) for p, y in zip(predictions, outcomes, strict=True) if low <= p < high or (index == bins - 1 and p == 1)]
        if not bucket:
            continue
        pred_mean = sum(p for p, _ in bucket) / len(bucket)
        obs_mean = sum(y for _, y in bucket) / len(bucket)
        ece += (len(bucket) / n) * abs(pred_mean - obs_mean)
    return CalibrationMetrics(brier, loss, ece, sharpness, n)


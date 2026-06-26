"""
Operational confidence score — Phase 1.

Answers: "How much should we trust this prediction for betting decisions?"
Range: 0.0 (very uncertain) → 1.0 (high confidence).

This is NOT the probability of the match outcome. It measures prediction quality
based on data availability and model calibration status.
"""

from __future__ import annotations


def compute_confidence_score(
    feature_completeness: float,
    calibration_available: bool,
    calibration_ece: float | None,
    market_quality_score: float,
    odds_age_minutes: float,
) -> float:
    """
    Weights:
      30% feature data quality
      25% calibration quality
      25% market quality
      20% odds freshness
    """
    score = 0.0

    # Feature data quality (30%)
    score += 0.30 * max(0.0, min(float(feature_completeness), 1.0))

    # Calibration quality (25%)
    if calibration_available:
        if calibration_ece is not None:
            # ECE 0 → 1.0 score; ECE ≥ 0.15 → 0.0
            cal_quality = max(0.0, 1.0 - calibration_ece / 0.15)
            score += 0.25 * cal_quality
        else:
            score += 0.10  # calibrated but no ECE metric yet

    # Market quality (25%)
    score += 0.25 * max(0.0, min(float(market_quality_score), 1.0))

    # Odds freshness (20%)
    if odds_age_minutes < 30:
        freshness = 1.0
    elif odds_age_minutes < 60:
        freshness = 0.7
    elif odds_age_minutes < 120:
        freshness = 0.3
    else:
        freshness = 0.0
    score += 0.20 * freshness

    return round(min(max(score, 0.0), 1.0), 4)

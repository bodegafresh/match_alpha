from __future__ import annotations

import json
from typing import Any

from app.core.time import ensure_aware_utc
from app.decision.ev_calculator import calculate_ev

_HARD_BLOCK_REASONS = {
    "ODDS_CAPTURED_AFTER_KICKOFF",
    "ODDS_STALE",
    "NO_ODDS_AVAILABLE",
    "COMPETITION_NOT_BETTABLE",
    "MISSING_PROBABILITY",
}


def evaluate_decision(
    candidate: dict[str, Any],
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    block_reasons: list[str] = []

    # --- Hard blocks ---
    kickoff_at = ensure_aware_utc(candidate["kickoff_at"])
    captured_at = ensure_aware_utc(candidate["captured_at"])
    if captured_at >= kickoff_at:
        block_reasons.append("ODDS_CAPTURED_AFTER_KICKOFF")

    if candidate.get("competition_status") != "BETTABLE":
        block_reasons.append("COMPETITION_NOT_BETTABLE")

    if market_context:
        if market_context.get("odds_age_minutes", 0) > 120:
            block_reasons.append("ODDS_STALE")
        if market_context.get("liquidity_tier") == "NONE":
            block_reasons.append("NO_ODDS_AVAILABLE")

    # --- Probability selection ---
    calibrated = candidate.get("calibrated_probability")
    raw = candidate.get("raw_probability")
    prediction_status = candidate.get("prediction_status", "RAW_ONLY")

    if calibrated is not None:
        prob_used = float(calibrated)
        has_calibration = True
    elif raw is not None:
        prob_used = float(raw)
        has_calibration = False
        block_reasons.append("NO_CALIBRATION")
    else:
        block_reasons.append("MISSING_PROBABILITY")
        return _blocked(candidate, block_reasons)

    # Hard-block gate
    hard_blocks = [r for r in block_reasons if r in _HARD_BLOCK_REASONS]
    if hard_blocks:
        return _blocked(candidate, block_reasons)

    # --- EV calculation ---
    result = calculate_ev(prob_used, float(candidate["decimal_odds"]))

    if result.ev <= 0:
        return {
            **_base(candidate, result, prob_used),
            "decision_status": "NO_EDGE",
            "risk_level": "LOW",
            "block_reasons": block_reasons,
            "block_reason": None,  # legacy field
        }

    # --- Soft checks (don't block, downgrade or annotate) ---
    confidence = float(candidate.get("confidence_score") or 0.0)
    if confidence < 0.3:
        block_reasons.append("LOW_CONFIDENCE")

    if market_context and market_context.get("liquidity_tier") == "LOW":
        block_reasons.append("LOW_LIQUIDITY")

    # --- BETTABLE vs PAPER_ONLY ---
    remaining_hard = [r for r in block_reasons if r in _HARD_BLOCK_REASONS]
    if has_calibration and not remaining_hard:
        status = "BETTABLE"
        risk = "MEDIUM"
    else:
        status = "PAPER_ONLY"
        risk = "LOW"

    return {
        **_base(candidate, result, prob_used),
        "decision_status": status,
        "risk_level": risk,
        "block_reasons": block_reasons,
        "block_reason": block_reasons[0] if block_reasons else None,  # legacy
    }


def _base(candidate: dict[str, Any], result: Any, prob_used: float) -> dict[str, Any]:
    return {
        "competition_season_id": candidate["competition_season_id"],
        "match_id": candidate["match_id"],
        "prediction_id": candidate["prediction_id"],
        "odds_snapshot_id": candidate["odds_snapshot_id"],
        "calibrated_probability_used": prob_used,
        "market_probability": result.market_probability,
        "edge": result.edge,
        "ev": result.ev,
        "kelly_fraction": result.kelly_fraction,
        "stake_fraction": result.stake_fraction,
        "payload": {"source": "python-decision-engine"},
    }


def _blocked(candidate: dict[str, Any], block_reasons: list[str], result: Any | None = None) -> dict[str, Any]:
    prob = candidate.get("calibrated_probability") or candidate.get("raw_probability")
    if result is None and prob is not None:
        result = calculate_ev(float(prob), float(candidate["decimal_odds"]))
    if result is None:
        result = type("EmptyResult", (), {
            "market_probability": None, "edge": None, "ev": None,
            "kelly_fraction": None, "stake_fraction": None,
        })()
    return {
        **_base(candidate, result, float(prob) if prob is not None else 0.0),
        "decision_status": "BLOCKED",
        "risk_level": "HIGH",
        "block_reasons": block_reasons,
        "block_reason": block_reasons[0] if block_reasons else None,  # legacy
    }

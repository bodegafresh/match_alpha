from datetime import datetime
from typing import Any

from app.core.time import ensure_aware_utc
from app.decision.ev_calculator import calculate_ev


def evaluate_decision(candidate: dict[str, Any]) -> dict[str, Any]:
    calibrated_probability = candidate.get("calibrated_probability")
    kickoff_at = ensure_aware_utc(candidate["kickoff_at"])
    captured_at = ensure_aware_utc(candidate["captured_at"])
    if captured_at >= kickoff_at:
        return blocked(candidate, "BLOCKED_ODDS_CAPTURED_AFTER_KICKOFF")
    if calibrated_probability is None:
        return blocked(candidate, "BLOCKED_MISSING_CALIBRATED_PROBABILITY")
    result = calculate_ev(float(calibrated_probability), float(candidate["decimal_odds"]))
    if candidate.get("competition_status") != "BETTABLE":
        return blocked(candidate, "BLOCKED_COMPETITION_NOT_BETTABLE", result)
    if result.ev <= 0:
        return {
            **base(candidate, result),
            "decision_status": "NO_EDGE",
            "risk_level": "LOW",
            "block_reason": None,
        }
    return {
        **base(candidate, result),
        "decision_status": "BETTABLE",
        "risk_level": "MEDIUM",
        "block_reason": None,
    }


def base(candidate: dict[str, Any], result: Any) -> dict[str, Any]:
    return {
        "competition_season_id": candidate["competition_season_id"],
        "match_id": candidate["match_id"],
        "prediction_id": candidate["prediction_id"],
        "odds_snapshot_id": candidate["odds_snapshot_id"],
        "calibrated_probability_used": candidate["calibrated_probability"],
        "market_probability": result.market_probability,
        "edge": result.edge,
        "ev": result.ev,
        "kelly_fraction": result.kelly_fraction,
        "stake_fraction": result.stake_fraction,
        "payload": {"source": "python-decision-engine"},
    }


def blocked(candidate: dict[str, Any], reason: str, result: Any | None = None) -> dict[str, Any]:
    if result is None and candidate.get("calibrated_probability") is not None:
        result = calculate_ev(float(candidate["calibrated_probability"]), float(candidate["decimal_odds"]))
    if result is None:
        result = type("EmptyResult", (), {"market_probability": None, "edge": None, "ev": None, "kelly_fraction": None, "stake_fraction": None})()
    return {
        **base(candidate, result),
        "decision_status": "BLOCKED",
        "risk_level": "HIGH",
        "block_reason": reason,
    }


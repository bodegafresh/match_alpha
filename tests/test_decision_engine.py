from datetime import UTC, datetime

import pytest

from app.decision.decision_engine import evaluate_decision
from app.decision.ev_calculator import calculate_ev


def candidate(**overrides):
    base = {
        "competition_season_id": "season",
        "match_id": "match",
        "prediction_id": "prediction",
        "odds_snapshot_id": "odds",
        "calibrated_probability": 0.6,
        "decimal_odds": 2.1,
        "kickoff_at": datetime(2026, 6, 25, 20, 0, tzinfo=UTC),
        "captured_at": datetime(2026, 6, 25, 10, 0, tzinfo=UTC),
        "competition_status": "BETTABLE",
    }
    base.update(overrides)
    return base


def test_ev_requires_calibrated_probability() -> None:
    with pytest.raises(ValueError):
        calculate_ev(None, 2.0)


def test_decision_blocks_post_kickoff_odds() -> None:
    result = evaluate_decision(candidate(captured_at=datetime(2026, 6, 25, 21, 0, tzinfo=UTC)))
    assert result["decision_status"] == "BLOCKED"
    assert result["block_reason"] == "BLOCKED_ODDS_CAPTURED_AFTER_KICKOFF"


def test_decision_blocks_non_bettable_competition() -> None:
    result = evaluate_decision(candidate(competition_status="PAPER_TRADING"))
    assert result["decision_status"] == "BLOCKED"
    assert result["block_reason"] == "BLOCKED_COMPETITION_NOT_BETTABLE"


def test_decision_can_be_bettable_with_positive_ev() -> None:
    result = evaluate_decision(candidate())
    assert result["decision_status"] == "BETTABLE"
    assert result["ev"] > 0


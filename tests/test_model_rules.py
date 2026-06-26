from datetime import UTC, datetime, timedelta

import pytest

from app.datasets.builder import build_dataset_version
from app.models.promotion_rules import should_promote_challenger


def test_dataset_builder_excludes_future_rows() -> None:
    as_of = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    rows = [
        {"match_id": "past", "kickoff_at": as_of - timedelta(days=1)},
        {"match_id": "future", "kickoff_at": as_of + timedelta(days=1)},
    ]
    dataset = build_dataset_version("season", "1X2", as_of, rows)
    assert [row["match_id"] for row in dataset.rows] == ["past"]


def test_dataset_builder_rejects_naive_prediction_time() -> None:
    with pytest.raises(ValueError):
        build_dataset_version("season", "1X2", datetime(2026, 6, 25, 12, 0), [])


def test_promotion_blocks_bad_challenger() -> None:
    decision = should_promote_challenger(
        champion_brier=0.2,
        challenger_brier=0.21,
        champion_log_loss=0.55,
        challenger_log_loss=0.57,
        challenger_ece=0.09,
        max_ece=0.08,
        sample_size=20,
        min_sample_size=50,
        severe_drift_open=True,
    )
    assert not decision.promote
    assert "sample_size_below_minimum" in decision.reasons
    assert "severe_drift_open" in decision.reasons


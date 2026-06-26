import pytest

from app.models.prediction_validation import validate_prediction_payload


def test_prediction_requires_feature_snapshot_id() -> None:
    with pytest.raises(ValueError):
        validate_prediction_payload({"model_run_id": "run"})


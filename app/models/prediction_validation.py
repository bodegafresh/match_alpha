from typing import Any


def validate_prediction_payload(row: dict[str, Any]) -> None:
    if not row.get("feature_snapshot_id"):
        raise ValueError("feature_snapshot_id is required before inserting model_predictions")


from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.hashing import sha256_json
from app.core.time import ensure_aware_utc


@dataclass(frozen=True)
class DatasetBuild:
    dataset_version: str
    source_hash: str
    rows: list[dict[str, Any]]


def build_dataset_version(competition_season_id: str, market_code: str, prediction_as_of: datetime, rows: list[dict[str, Any]]) -> DatasetBuild:
    prediction_as_of = ensure_aware_utc(prediction_as_of)
    eligible = [
        row for row in rows
        if row.get("kickoff_at") and ensure_aware_utc(row["kickoff_at"]) < prediction_as_of
    ]
    payload = {
        "competition_season_id": competition_season_id,
        "market_code": market_code,
        "prediction_as_of": prediction_as_of.isoformat(),
        "rows": eligible,
    }
    source_hash = sha256_json(payload)
    return DatasetBuild(dataset_version=f"dataset-{source_hash[:12]}", source_hash=source_hash, rows=eligible)


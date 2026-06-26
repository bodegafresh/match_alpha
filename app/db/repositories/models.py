import json
from typing import Any

from app.core.hashing import sha256_json
from app.core.time import utc_now
from app.db.repositories.base import Repository
from app.models.prediction_validation import validate_prediction_payload


class ModelRepository(Repository):
    async def create_model_run(
        self,
        *,
        model_id: str,
        competition_season_id: str,
        market_id: str,
        prediction_as_of: str,
        feature_set_version: str,
        dataset_version: str,
        git_sha: str | None,
        params: dict[str, Any],
    ) -> str:
        params = dict(params)
        params.setdefault("config_hash", sha256_json(params))
        row = await self.fetch_one(
            """
            insert into model_runs (
              model_id, competition_season_id, market_id, run_status,
              prediction_as_of, feature_set_version, dataset_version, git_sha, params
            )
            values (
              :model_id, :competition_season_id, :market_id, 'STARTED',
              cast(:prediction_as_of as timestamptz), :feature_set_version,
              :dataset_version, :git_sha, cast(:params as jsonb)
            )
            returning model_run_id::text
            """,
            {
                "model_id": model_id,
                "competition_season_id": competition_season_id,
                "market_id": market_id,
                "prediction_as_of": prediction_as_of,
                "feature_set_version": feature_set_version,
                "dataset_version": dataset_version,
                "git_sha": git_sha,
                "params": json.dumps(params),
            },
        )
        return row["model_run_id"]

    async def insert_prediction(self, row: dict[str, Any]) -> str:
        validate_prediction_payload(row)
        result = await self.fetch_one(
            """
            insert into model_predictions (
              model_run_id, feature_snapshot_id, competition_season_id, match_id,
              market_id, selection_id, line, raw_probability, calibrated_probability,
              fair_odds, as_of, flags, payload
            )
            values (
              :model_run_id, :feature_snapshot_id, :competition_season_id, :match_id,
              :market_id, :selection_id, :line, :raw_probability, :calibrated_probability,
              :fair_odds, cast(:as_of as timestamptz), :flags, cast(:payload as jsonb)
            )
            returning prediction_id::text
            """,
            {
                **row,
                "as_of": row.get("as_of") or utc_now().isoformat(),
                "flags": row.get("flags", []),
                "payload": json.dumps(row.get("payload", {})),
            },
        )
        return result["prediction_id"]

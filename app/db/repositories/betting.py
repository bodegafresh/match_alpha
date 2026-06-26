import json
from typing import Any

from app.db.repositories.base import Repository


class BettingRepository(Repository):
    async def eligible_prediction_odds(self) -> list[dict[str, Any]]:
        return await self.fetch_all(
            """
            select
              p.prediction_id::text,
              p.competition_season_id::text,
              p.match_id::text,
              p.calibrated_probability,
              p.market_id::text,
              p.selection_id::text,
              p.line,
              m.kickoff_at,
              os.odds_snapshot_id::text,
              os.decimal_odds,
              os.implied_probability,
              os.captured_at,
              coalesce(cs_status.status::text, 'OBSERVATION') as competition_status
            from model_predictions p
            join matches m on m.match_id = p.match_id
            join odds_snapshots os
              on os.match_id = p.match_id
             and os.market_id = p.market_id
             and os.selection_id = p.selection_id
             and coalesce(os.line, -999999) = coalesce(p.line, -999999)
            left join competition_status cs_status
              on cs_status.competition_season_id = p.competition_season_id
            where p.calibrated_probability is not null
              and os.captured_at < m.kickoff_at
            """
        )

    async def insert_decision(self, row: dict[str, Any]) -> str:
        result = await self.fetch_one(
            """
            insert into betting_decisions (
              competition_season_id, match_id, prediction_id, odds_snapshot_id,
              decision_status, risk_level, block_reason, calibrated_probability_used,
              market_probability, edge, ev, kelly_fraction, stake_fraction, payload
            )
            values (
              :competition_season_id, :match_id, :prediction_id, :odds_snapshot_id,
              cast(:decision_status as betting_decision_status), cast(:risk_level as risk_level),
              :block_reason, :calibrated_probability_used, :market_probability, :edge, :ev,
              :kelly_fraction, :stake_fraction, cast(:payload as jsonb)
            )
            returning betting_decision_id::text
            """,
            {**row, "payload": json.dumps(row.get("payload", {}))},
        )
        return result["betting_decision_id"]

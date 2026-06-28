import json
from typing import Any

from app.db.repositories.base import Repository


class BettingRepository(Repository):
    async def eligible_prediction_odds(self) -> list[dict[str, Any]]:
        """Returns one candidate per prediction using the latest pre-kickoff odds snapshot."""
        return await self.fetch_all(
            """
            select distinct on (p.match_id, p.market_id, p.selection_id)
              p.prediction_id::text,
              p.competition_season_id::text,
              p.match_id::text,
              p.calibrated_probability,
              p.raw_probability,
              p.prediction_status::text,
              p.confidence_score,
              p.market_id::text,
              p.selection_id::text,
              p.line,
              m.kickoff_at,
              latest_os.odds_snapshot_id::text,
              latest_os.decimal_odds,
              latest_os.implied_probability,
              latest_os.captured_at,
              coalesce(cs_status.status::text, 'OBSERVATION') as competition_status
            from model_predictions p
            join matches m on m.match_id = p.match_id
            join lateral (
              select *
              from odds_snapshots os
              where os.match_id = p.match_id
                and os.market_id = p.market_id
                and os.selection_id = p.selection_id
                and coalesce(os.line, -999999) = coalesce(p.line, -999999)
                and os.captured_at < m.kickoff_at
              order by os.captured_at desc
              limit 1
            ) latest_os on true
            left join competition_status cs_status
              on cs_status.competition_season_id = p.competition_season_id
            where (p.calibrated_probability is not null or p.raw_probability is not null)
            order by p.match_id, p.market_id, p.selection_id, p.as_of desc
            """,
        )

    async def insert_decision(self, row: dict[str, Any]) -> str:
        """Upserts a betting decision. Safe to call multiple times for the same candidate."""
        block_reasons = row.get("block_reasons") or []
        result = await self.fetch_one(
            """
            insert into betting_decisions (
              competition_season_id, match_id, prediction_id, odds_snapshot_id,
              decision_status, risk_level, block_reason, block_reasons, calibrated_probability_used,
              market_probability, edge, ev, kelly_fraction, stake_fraction, payload
            )
            values (
              :competition_season_id, :match_id, :prediction_id, :odds_snapshot_id,
              cast(:decision_status as betting_decision_status), cast(:risk_level as risk_level),
              :block_reason, cast(:block_reasons as jsonb), :calibrated_probability_used,
              :market_probability, :edge, :ev, :kelly_fraction, :stake_fraction, cast(:payload as jsonb)
            )
            on conflict (prediction_id, odds_snapshot_id)
            do update set
              decision_status = excluded.decision_status,
              risk_level = excluded.risk_level,
              block_reason = excluded.block_reason,
              block_reasons = excluded.block_reasons,
              edge = excluded.edge,
              ev = excluded.ev,
              kelly_fraction = excluded.kelly_fraction,
              stake_fraction = excluded.stake_fraction,
              payload = excluded.payload
            returning betting_decision_id::text
            """,
            {
                **row,
                "block_reason": block_reasons[0] if block_reasons else None,
                "block_reasons": json.dumps(block_reasons),
                "payload": json.dumps(row.get("payload", {})),
            },
        )
        return result["betting_decision_id"]

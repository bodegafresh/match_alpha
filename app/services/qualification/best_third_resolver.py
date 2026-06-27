"""
BestThirdPlaceResolver: ranks the 12 third-place teams and qualifies the top 8.

FIFA 2026 ranking rules for best thirds:
  1. points
  2. goal_difference
  3. goals_for
  4. fair_play_points     (not yet stored → PENDING_TIEBREAKER)
  5. drawing_of_lots      (not resolvable → PENDING_TIEBREAKER)

Top 8 thirds → QUALIFIED_BEST_THIRD
Rest → ELIMINATED
Unresolvable ties → PENDING_TIEBREAKER (data quality event written)
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.qualification.models import (
    ELIMINATED,
    PENDING_TIEBREAKER,
    QUALIFIED_BEST_THIRD,
    THIRD_PLACE_CANDIDATE,
    BestThirdEntry,
)

log = logging.getLogger(__name__)

DEFAULT_THIRD_RULES = [
    "points",
    "goal_difference",
    "goals_for",
]


class BestThirdPlaceResolver:
    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    async def resolve(self, competition_season_id: str) -> dict[str, Any]:
        """
        Read THIRD_PLACE_CANDIDATE standings, rank them, qualify top 8.
        Returns ranked list and count of qualified/pending/eliminated.
        """
        stage_rules = await self._get_group_stage_rules(competition_season_id)
        n_qualifiers = stage_rules.get("qualifies", {}).get("best_third_places", 8)
        ranking_rules = stage_rules.get("qualification", {}).get(
            "third_place_ranking_rules", DEFAULT_THIRD_RULES
        )

        thirds = await self._get_third_place_candidates(competition_season_id)
        if not thirds:
            return {"thirds_total": 0, "qualified": 0, "eliminated": 0, "pending": 0}

        ranked = self._rank_thirds(thirds, ranking_rules)

        qualified: list[BestThirdEntry] = []
        eliminated: list[BestThirdEntry] = []
        pending: list[BestThirdEntry] = []

        for i, entry in enumerate(ranked):
            entry.rank = i + 1
            if i < n_qualifiers:
                # Check for unresolvable tie at the cutoff boundary
                if entry.qualification_status == PENDING_TIEBREAKER:
                    pending.append(entry)
                else:
                    entry.qualification_status = QUALIFIED_BEST_THIRD
                    qualified.append(entry)
            else:
                if entry.qualification_status == PENDING_TIEBREAKER:
                    pending.append(entry)
                else:
                    entry.qualification_status = ELIMINATED
                    eliminated.append(entry)

        await self._update_standings(competition_season_id, ranked)

        return {
            "thirds_total": len(thirds),
            "qualified": len(qualified),
            "eliminated": len(eliminated),
            "pending": len(pending),
            "ranked": [
                {
                    "rank": e.rank,
                    "team_id": e.team_id,
                    "team_name": e.team_name,
                    "group_code": e.group_code,
                    "points": e.points,
                    "goal_difference": e.goal_difference,
                    "goals_for": e.goals_for,
                    "qualification_status": e.qualification_status,
                }
                for e in ranked
            ],
        }

    # ── ranking ───────────────────────────────────────────────────────────────

    def _rank_thirds(self, thirds: list[BestThirdEntry], rules: list[str]) -> list[BestThirdEntry]:
        """Sort thirds by rules. Flag ties that can't be resolved as PENDING_TIEBREAKER."""
        thirds.sort(
            key=lambda t: (-t.points, -t.goal_difference, -t.goals_for, t.team_id)
        )

        # Detect adjacent ties and flag unresolvable ones
        for i in range(len(thirds) - 1):
            a, b = thirds[i], thirds[i + 1]
            if (
                a.points == b.points
                and a.goal_difference == b.goal_difference
                and a.goals_for == b.goals_for
            ):
                # fair_play not stored yet — mark as pending
                if "fair_play_points" not in rules:
                    a.qualification_status = PENDING_TIEBREAKER
                    b.qualification_status = PENDING_TIEBREAKER
                    log.warning(
                        "best_third_resolver: unresolvable tie between %s and %s",
                        a.team_name, b.team_name,
                    )

        return thirds

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _get_group_stage_rules(self, competition_season_id: str) -> dict:
        result = await self.conn.execute(
            text("""
                SELECT st.rules
                FROM competition_stages st
                WHERE st.competition_season_id = cast(:sid as uuid)
                  AND st.stage_type = 'GROUP_STAGE'
                LIMIT 1
            """),
            {"sid": competition_season_id},
        )
        row = result.fetchone()
        return row[0] if row and row[0] else {}

    async def _get_third_place_candidates(
        self, competition_season_id: str
    ) -> list[BestThirdEntry]:
        result = await self.conn.execute(
            text("""
                SELECT DISTINCT ON (s.group_id, s.team_id)
                    s.team_id::text,
                    t.display_name AS team_name,
                    cg.group_code,
                    s.points,
                    s.goal_difference,
                    s.goals_for
                FROM standings s
                JOIN teams t  ON t.team_id  = s.team_id
                JOIN competition_groups cg ON cg.group_id = s.group_id
                WHERE s.competition_season_id = cast(:sid as uuid)
                  AND s.qualification_status = 'THIRD_PLACE_CANDIDATE'
                  AND s.group_id IS NOT NULL
                ORDER BY s.group_id, s.team_id, s.as_of DESC
            """),
            {"sid": competition_season_id},
        )
        return [
            BestThirdEntry(
                team_id=r.team_id,
                team_name=r.team_name,
                group_code=r.group_code,
                points=r.points or 0,
                goal_difference=r.goal_difference or 0,
                goals_for=r.goals_for or 0,
            )
            for r in result
        ]

    async def _update_standings(
        self, competition_season_id: str, ranked: list[BestThirdEntry]
    ) -> None:
        for entry in ranked:
            await self.conn.execute(
                text("""
                    UPDATE standings SET qualification_status = :qs
                    WHERE competition_season_id = cast(:sid as uuid)
                      AND team_id = cast(:tid as uuid)
                      AND qualification_status IN (
                          'THIRD_PLACE_CANDIDATE',
                          'QUALIFIED_BEST_THIRD',
                          'PENDING_TIEBREAKER'
                      )
                """),
                {"sid": competition_season_id, "tid": entry.team_id, "qs": entry.qualification_status},
            )

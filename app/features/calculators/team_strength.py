"""
Normalized rolling attack/defense strength.

attack_strength  = team_goals_scored_avg / competition_goals_scored_avg
defense_strength = team_goals_conceded_avg / competition_goals_conceded_avg

Values > 1.0 = above average attack / above average defensive vulnerability.
Values < 1.0 = below average.

NOTE: This is NOT Dixon-Coles. Dixon-Coles requires MLE optimization with rho
correction for correlated low-score results. That belongs in Phase 3.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now

_WC_DEFAULT_AVG_GOALS = 1.3


async def _get_team_recent_matches(
    conn: AsyncConnection,
    team_id: str,
    competition_season_id: str,
    before_kickoff: Any,
    window: int,
) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text("""
            SELECT
              mp.score AS goals_for,
              opp.score AS goals_against
            FROM matches m
            JOIN match_participants mp
              ON mp.match_id = m.match_id
             AND mp.team_id = cast(:team_id as uuid)
            JOIN match_participants opp
              ON opp.match_id = m.match_id
             AND opp.team_id != cast(:team_id as uuid)
            WHERE m.competition_season_id = cast(:season_id as uuid)
              AND m.status = 'FINISHED'
              AND m.kickoff_at < :before_kickoff
              AND mp.score IS NOT NULL
              AND opp.score IS NOT NULL
            ORDER BY m.kickoff_at DESC
            LIMIT :window
        """),
        {
            "team_id": team_id,
            "season_id": competition_season_id,
            "before_kickoff": before_kickoff,
            "window": window,
        },
    )
    return [dict(r._mapping) for r in rows]


async def _get_competition_avg_goals(
    conn: AsyncConnection,
    competition_season_id: str,
    before_kickoff: Any,
) -> tuple[float, float]:
    row = await conn.execute(
        text("""
            SELECT
              avg(mp.score)  AS avg_scored,
              avg(opp.score) AS avg_conceded
            FROM matches m
            JOIN match_participants mp
              ON mp.match_id = m.match_id AND mp.side = 'HOME'
            JOIN match_participants opp
              ON opp.match_id = m.match_id AND opp.side = 'AWAY'
            WHERE m.competition_season_id = cast(:season_id as uuid)
              AND m.status = 'FINISHED'
              AND m.kickoff_at < :before_kickoff
              AND mp.score IS NOT NULL
              AND opp.score IS NOT NULL
        """),
        {"season_id": competition_season_id, "before_kickoff": before_kickoff},
    )
    r = row.fetchone()
    avg_scored = float(r[0]) if r and r[0] is not None else _WC_DEFAULT_AVG_GOALS
    avg_conceded = float(r[1]) if r and r[1] is not None else _WC_DEFAULT_AVG_GOALS
    return avg_scored, avg_conceded


async def compute_normalized_strength(
    conn: AsyncConnection,
    team_id: str,
    competition_season_id: str,
    before_kickoff: Any,
    window: int = 10,
) -> tuple[float, float]:
    """
    Returns (attack_strength, defense_strength).
    Both default to 1.0 (neutral) if insufficient data.
    """
    matches = await _get_team_recent_matches(
        conn, team_id, competition_season_id, before_kickoff, window
    )
    if not matches:
        return 1.0, 1.0

    avg_scored, avg_conceded = await _get_competition_avg_goals(
        conn, competition_season_id, before_kickoff
    )

    team_scored = sum(m["goals_for"] for m in matches) / len(matches)
    team_conceded = sum(m["goals_against"] for m in matches) / len(matches)

    attack_strength = team_scored / avg_scored if avg_scored > 0 else 1.0
    defense_strength = team_conceded / avg_conceded if avg_conceded > 0 else 1.0

    return round(attack_strength, 4), round(defense_strength, 4)

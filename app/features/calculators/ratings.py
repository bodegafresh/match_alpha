"""
ELO rating engine — three types: GLOBAL, INTERNATIONAL, DOMESTIC.

Two operation modes:
  - update_elo_from_recent_matches(): incremental, production daily use.
    Processes only matches WHERE elo_processed = false AND status = 'FINISHED'.
    Marks elo_processed = true AFTER all three rating types are computed.
  - rebuild_all_elo_history(): full rebuild, admin/bootstrap only.
    Resets elo_processed = false for all finished matches, then reprocesses all.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now
from app.models.elo_model import elo_expected, elo_update

_DEFAULT_RATING = 1500.0
_DEFAULT_K = 20.0

# K-factors by stage type and ELO context
_K_FACTORS: dict[str, dict[str, float]] = {
    "ELO_GLOBAL": {
        "FINAL": 40.0, "THIRD_PLACE": 35.0,
        "SEMI_FINAL": 35.0, "QUARTER_FINAL": 32.0,
        "ROUND_OF_16": 30.0, "ROUND_OF_32": 28.0,
        "GROUP_STAGE": 25.0, "LEAGUE_PHASE": 20.0,
        "FRIENDLY": 10.0, "DEFAULT": 20.0,
    },
    "ELO_INTERNATIONAL": {
        "FINAL": 45.0, "THIRD_PLACE": 40.0,
        "SEMI_FINAL": 40.0, "QUARTER_FINAL": 36.0,
        "ROUND_OF_16": 32.0, "ROUND_OF_32": 30.0,
        "GROUP_STAGE": 28.0, "LEAGUE_PHASE": 22.0,
        "FRIENDLY": 8.0, "DEFAULT": 22.0,
    },
    "ELO_DOMESTIC": {
        "FINAL": 35.0, "THIRD_PLACE": 30.0,
        "SEMI_FINAL": 32.0, "QUARTER_FINAL": 28.0,
        "ROUND_OF_16": 25.0, "ROUND_OF_32": 22.0,
        "GROUP_STAGE": 20.0, "LEAGUE_PHASE": 18.0,
        "FRIENDLY": 8.0, "DEFAULT": 18.0,
    },
}

_INTERNATIONAL_TYPES = {"INTERNATIONAL", "INTERNATIONAL_CUP"}
_DOMESTIC_TYPES = {"DOMESTIC", "DOMESTIC_CUP", "CLUB_INTERNATIONAL"}


def _k_factor(stage_type: str | None, elo_type: str) -> float:
    table = _K_FACTORS.get(elo_type, _K_FACTORS["ELO_GLOBAL"])
    return table.get(stage_type or "DEFAULT", table["DEFAULT"])


def _match_result(home_score: int, away_score: int) -> float:
    if home_score > away_score:
        return 1.0
    if home_score < away_score:
        return 0.0
    return 0.5


async def get_latest_rating(
    conn: AsyncConnection,
    team_id: str,
    rating_type: str,
    before: datetime,
) -> float | None:
    row = await conn.execute(
        text("""
            SELECT rating_value FROM rating_snapshots
            WHERE team_id = cast(:team_id as uuid)
              AND rating_type = :rating_type
              AND as_of <= :before
            ORDER BY as_of DESC
            LIMIT 1
        """),
        {"team_id": team_id, "rating_type": rating_type, "before": before},
    )
    r = row.fetchone()
    return float(r[0]) if r else None


async def upsert_rating_snapshot(
    conn: AsyncConnection,
    team_id: str,
    rating_type: str,
    rating_value: float,
    as_of: datetime,
    competition_season_id: str | None = None,
) -> None:
    await conn.execute(
        text("""
            INSERT INTO rating_snapshots (competition_season_id, team_id, rating_type, rating_value, as_of)
            VALUES (cast(:cs_id as uuid), cast(:team_id as uuid), :rating_type, :rating_value, :as_of)
            ON CONFLICT (competition_season_id, team_id, rating_type, as_of)
            DO UPDATE SET rating_value = excluded.rating_value
        """),
        {
            "cs_id": competition_season_id,
            "team_id": team_id,
            "rating_type": rating_type,
            "rating_value": round(rating_value, 4),
            "as_of": as_of,
        },
    )


async def _fetch_unprocessed_matches(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text("""
            SELECT
              m.match_id::text,
              m.competition_season_id::text,
              m.kickoff_at,
              m.home_score,
              m.away_score,
              home_p.team_id::text  AS home_team_id,
              away_p.team_id::text  AS away_team_id,
              c.competition_type,
              st.stage_type
            FROM matches m
            JOIN match_participants home_p
              ON home_p.match_id = m.match_id AND home_p.side = 'HOME'
            JOIN match_participants away_p
              ON away_p.match_id = m.match_id AND away_p.side = 'AWAY'
            JOIN competition_seasons cs
              ON cs.competition_season_id = m.competition_season_id
            JOIN competitions c
              ON c.competition_id = cs.competition_id
            LEFT JOIN competition_stages st
              ON st.stage_id = m.stage_id
            WHERE m.elo_processed = false
              AND m.status = 'FINISHED'
              AND m.home_score IS NOT NULL
              AND m.away_score IS NOT NULL
            ORDER BY m.kickoff_at ASC
        """)
    )
    return [dict(r._mapping) for r in rows]


async def _fetch_all_finished_matches(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text("""
            SELECT
              m.match_id::text,
              m.competition_season_id::text,
              m.kickoff_at,
              m.home_score,
              m.away_score,
              home_p.team_id::text  AS home_team_id,
              away_p.team_id::text  AS away_team_id,
              c.competition_type,
              st.stage_type
            FROM matches m
            JOIN match_participants home_p
              ON home_p.match_id = m.match_id AND home_p.side = 'HOME'
            JOIN match_participants away_p
              ON away_p.match_id = m.match_id AND away_p.side = 'AWAY'
            JOIN competition_seasons cs
              ON cs.competition_season_id = m.competition_season_id
            JOIN competitions c
              ON c.competition_id = cs.competition_id
            LEFT JOIN competition_stages st
              ON st.stage_id = m.stage_id
            WHERE m.status = 'FINISHED'
              AND m.home_score IS NOT NULL
              AND m.away_score IS NOT NULL
            ORDER BY m.kickoff_at ASC
        """)
    )
    return [dict(r._mapping) for r in rows]


async def _process_matches_for_elo_type(
    conn: AsyncConnection,
    matches: list[dict[str, Any]],
    elo_type: str,
    ratings: dict[str, float],
) -> None:
    for match in matches:
        comp_type = match.get("competition_type") or ""
        home_id = match["home_team_id"]
        away_id = match["away_team_id"]
        kickoff = match["kickoff_at"]
        stage_type = match.get("stage_type")
        cs_id = match.get("competition_season_id")

        if elo_type == "ELO_INTERNATIONAL" and comp_type not in _INTERNATIONAL_TYPES:
            continue
        if elo_type == "ELO_DOMESTIC" and comp_type in _INTERNATIONAL_TYPES:
            continue

        home_r = ratings.get(home_id, _DEFAULT_RATING)
        away_r = ratings.get(away_id, _DEFAULT_RATING)

        # Store PRE-match snapshot (this is what features will read)
        pre_kickoff = kickoff - timedelta(seconds=1)
        await upsert_rating_snapshot(conn, home_id, elo_type, home_r, pre_kickoff, cs_id)
        await upsert_rating_snapshot(conn, away_id, elo_type, away_r, pre_kickoff, cs_id)

        result = _match_result(match["home_score"], match["away_score"])
        k = _k_factor(stage_type, elo_type)

        ratings[home_id] = elo_update(home_r, away_r, result, k)
        ratings[away_id] = elo_update(away_r, home_r, 1.0 - result, k)

        # Store POST-match snapshot
        await upsert_rating_snapshot(conn, home_id, elo_type, ratings[home_id], kickoff, cs_id)
        await upsert_rating_snapshot(conn, away_id, elo_type, ratings[away_id], kickoff, cs_id)


async def update_elo_from_recent_matches(conn: AsyncConnection) -> dict[str, Any]:
    """
    Incremental ELO update — production daily use.
    Only processes matches WHERE elo_processed = false AND status = 'FINISHED'.
    Marks elo_processed = true AFTER all rating types are computed.
    """
    matches = await _fetch_unprocessed_matches(conn)
    if not matches:
        return {"processed": 0, "rating_types": []}

    processed_ids = [m["match_id"] for m in matches]

    for elo_type in ("ELO_GLOBAL", "ELO_INTERNATIONAL", "ELO_DOMESTIC"):
        # Seed current ratings from DB for teams in these matches
        team_ids = {m["home_team_id"] for m in matches} | {m["away_team_id"] for m in matches}
        ratings: dict[str, float] = {}
        for team_id in team_ids:
            r = await get_latest_rating(conn, team_id, elo_type, before=matches[0]["kickoff_at"])
            ratings[team_id] = r if r is not None else _DEFAULT_RATING

        await _process_matches_for_elo_type(conn, matches, elo_type, ratings)

    # Mark ALL processed AFTER all rating types are done
    for match_id in processed_ids:
        await conn.execute(
            text("UPDATE matches SET elo_processed = true WHERE match_id = cast(:mid as uuid)"),
            {"mid": match_id},
        )

    return {"processed": len(processed_ids), "rating_types": ["ELO_GLOBAL", "ELO_INTERNATIONAL", "ELO_DOMESTIC"]}


async def rebuild_all_elo_history(conn: AsyncConnection) -> dict[str, Any]:
    """
    Full ELO rebuild from all historical matches — ADMIN / BOOTSTRAP ONLY.
    Resets elo_processed = false for all FINISHED matches, then reprocesses all.
    Do NOT call from production daily jobs.
    """
    await conn.execute(text("UPDATE matches SET elo_processed = false WHERE status = 'FINISHED'"))
    matches = await _fetch_all_finished_matches(conn)

    for elo_type in ("ELO_GLOBAL", "ELO_INTERNATIONAL", "ELO_DOMESTIC"):
        ratings: dict[str, float] = defaultdict(lambda: _DEFAULT_RATING)
        await _process_matches_for_elo_type(conn, matches, elo_type, dict(ratings))

    processed_ids = [m["match_id"] for m in matches]
    for match_id in processed_ids:
        await conn.execute(
            text("UPDATE matches SET elo_processed = true WHERE match_id = cast(:mid as uuid)"),
            {"mid": match_id},
        )

    return {"rebuilt": len(processed_ids), "rating_types": ["ELO_GLOBAL", "ELO_INTERNATIONAL", "ELO_DOMESTIC"]}

"""
Feature snapshot builder — Phase 1.

Materializes feature_snapshots for BOTH teams in a match.
Called by feature_snapshot_build job. NEVER called during prediction runtime.

All features are calculated as_of < kickoff_at (no leakage).
Critical features stored as real columns; secondary features in JSONB.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.hashing import sha256_json
from app.core.time import utc_now
from app.features.calculators.match_context import (
    feature_completeness,
    rest_days,
    stage_pressure,
)
from app.features.calculators.ratings import get_latest_rating
from app.features.calculators.team_strength import compute_normalized_strength
from app.models.elo_model import elo_expected

FEATURE_SET_VERSION = "v1"
_DEFAULT_ELO = 1500.0


async def _get_match_context(conn: AsyncConnection, match_id: str) -> dict[str, Any] | None:
    row = await conn.execute(
        text("""
            SELECT
              m.match_id::text,
              m.kickoff_at,
              m.competition_season_id::text,
              m.stage_id::text,
              home_p.team_id::text  AS home_team_id,
              away_p.team_id::text  AS away_team_id,
              st.stage_type,
              coalesce(v.is_neutral, false) AS is_neutral
            FROM matches m
            JOIN match_participants home_p
              ON home_p.match_id = m.match_id AND home_p.side = 'HOME'
            JOIN match_participants away_p
              ON away_p.match_id = m.match_id AND away_p.side = 'AWAY'
            LEFT JOIN competition_stages st ON st.stage_id = m.stage_id
            LEFT JOIN venues v ON v.venue_id = m.venue_id
            WHERE m.match_id = cast(:match_id as uuid)
        """),
        {"match_id": match_id},
    )
    r = row.fetchone()
    return dict(r._mapping) if r else None


async def _get_last_match_date(
    conn: AsyncConnection, team_id: str, before_kickoff: datetime
) -> datetime | None:
    row = await conn.execute(
        text("""
            SELECT m.kickoff_at
            FROM matches m
            JOIN match_participants mp
              ON mp.match_id = m.match_id AND mp.team_id = cast(:team_id as uuid)
            WHERE m.status = 'FINISHED'
              AND m.kickoff_at < :before_kickoff
            ORDER BY m.kickoff_at DESC
            LIMIT 1
        """),
        {"team_id": team_id, "before_kickoff": before_kickoff},
    )
    r = row.fetchone()
    return r[0] if r else None


async def _get_form(
    conn: AsyncConnection, team_id: str, before_kickoff: datetime, n: int = 5
) -> dict[str, Any]:
    rows = await conn.execute(
        text("""
            SELECT
              mp.score  AS goals_for,
              opp.score AS goals_against
            FROM matches m
            JOIN match_participants mp
              ON mp.match_id = m.match_id AND mp.team_id = cast(:team_id as uuid)
            JOIN match_participants opp
              ON opp.match_id = m.match_id AND opp.team_id != cast(:team_id as uuid)
            WHERE m.status = 'FINISHED'
              AND m.kickoff_at < :before_kickoff
              AND mp.score IS NOT NULL AND opp.score IS NOT NULL
            ORDER BY m.kickoff_at DESC
            LIMIT :n
        """),
        {"team_id": team_id, "before_kickoff": before_kickoff, "n": n},
    )
    matches = [dict(r._mapping) for r in rows]
    if not matches:
        return {"form_points": 0.0, "form_gd": 0.0, "sample_size": 0}

    def _pts(m: dict) -> float:
        if m["goals_for"] > m["goals_against"]:
            return 3.0
        if m["goals_for"] == m["goals_against"]:
            return 1.0
        return 0.0

    form_pts = sum(_pts(m) for m in matches) / len(matches)
    form_gd = sum(m["goals_for"] - m["goals_against"] for m in matches) / len(matches)
    return {"form_points": round(form_pts, 4), "form_gd": round(form_gd, 4), "sample_size": len(matches)}


async def _upsert_feature_snapshot(conn: AsyncConnection, data: dict[str, Any]) -> None:
    await conn.execute(
        text("""
            INSERT INTO feature_snapshots (
              competition_season_id, match_id, team_id, team_side,
              feature_set_version, as_of,
              elo_global, elo_international, elo_domestic, elo_diff,
              attack_strength, defense_strength,
              form_points, form_gd, rest_days,
              is_home, is_neutral, stage_pressure,
              feature_completeness, features, source_hash
            )
            VALUES (
              cast(:competition_season_id as uuid),
              cast(:match_id as uuid),
              cast(:team_id as uuid),
              :team_side,
              :feature_set_version,
              :as_of,
              :elo_global, :elo_international, :elo_domestic, :elo_diff,
              :attack_strength, :defense_strength,
              :form_points, :form_gd, :rest_days,
              :is_home, :is_neutral, :stage_pressure,
              :feature_completeness,
              cast(:features as jsonb),
              :source_hash
            )
            ON CONFLICT ON CONSTRAINT uq_feature_snapshots_key
            DO UPDATE SET
              elo_global           = excluded.elo_global,
              elo_international    = excluded.elo_international,
              elo_domestic         = excluded.elo_domestic,
              elo_diff             = excluded.elo_diff,
              attack_strength      = excluded.attack_strength,
              defense_strength     = excluded.defense_strength,
              form_points          = excluded.form_points,
              form_gd              = excluded.form_gd,
              rest_days            = excluded.rest_days,
              is_home              = excluded.is_home,
              is_neutral           = excluded.is_neutral,
              stage_pressure       = excluded.stage_pressure,
              feature_completeness = excluded.feature_completeness,
              features             = excluded.features,
              source_hash          = excluded.source_hash
            WHERE feature_snapshots.source_hash IS DISTINCT FROM excluded.source_hash
        """),
        data,
    )


async def build_match_feature_snapshots(
    conn: AsyncConnection,
    match_id: str,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """
    Build and materialize feature snapshots for both teams in a match.
    Returns {'home': critical_features, 'away': critical_features}.
    """
    if as_of is None:
        as_of = utc_now()

    ctx = await _get_match_context(conn, match_id)
    if not ctx:
        return {"error": "match_not_found", "match_id": match_id}

    kickoff = ctx["kickoff_at"]
    season_id = ctx["competition_season_id"]
    results: dict[str, Any] = {}

    for team_id, side, opponent_id in [
        (ctx["home_team_id"], "HOME", ctx["away_team_id"]),
        (ctx["away_team_id"], "AWAY", ctx["home_team_id"]),
    ]:
        elo_global = await get_latest_rating(conn, team_id, "ELO_GLOBAL", before=kickoff)
        elo_intl   = await get_latest_rating(conn, team_id, "ELO_INTERNATIONAL", before=kickoff)
        elo_dom    = await get_latest_rating(conn, team_id, "ELO_DOMESTIC", before=kickoff)
        opp_elo    = await get_latest_rating(conn, opponent_id, "ELO_GLOBAL", before=kickoff)

        elo_g  = elo_global if elo_global is not None else _DEFAULT_ELO
        opp_g  = opp_elo   if opp_elo   is not None else _DEFAULT_ELO
        elo_diff = round(elo_g - opp_g, 4)

        attack_str, defense_str = await compute_normalized_strength(
            conn, team_id, season_id, before_kickoff=kickoff
        )

        form = await _get_form(conn, team_id, kickoff, n=5)

        last_match_dt = await _get_last_match_date(conn, team_id, kickoff)
        rd = rest_days(kickoff.date(), last_match_dt.date() if last_match_dt else None)

        is_home    = side == "HOME"
        is_neutral = bool(ctx.get("is_neutral"))
        sp         = stage_pressure(ctx.get("stage_type"))

        completeness = feature_completeness({
            "elo_global": elo_global,
            "elo_diff": elo_diff,
            "attack_strength": attack_str,
            "defense_strength": defense_str,
            "form_sample_size": form["sample_size"],
            "last_match_date": last_match_dt,
        })

        secondary = {
            "win_prob_elo": round(elo_expected(elo_g, opp_g), 4),
            "form_sample_size": form["sample_size"],
            "feature_set_version": FEATURE_SET_VERSION,
            "lineup_available": False,
            "odds_available":   False,
            "weather_available": False,
        }

        critical = {
            "elo_global": elo_global,
            "elo_international": elo_intl,
            "elo_domestic": elo_dom,
            "elo_diff": elo_diff,
            "attack_strength": attack_str,
            "defense_strength": defense_str,
            "form_points": form["form_points"],
            "form_gd": form["form_gd"],
            "rest_days": rd,
            "is_home": is_home,
            "is_neutral": is_neutral,
            "stage_pressure": sp,
            "feature_completeness": completeness,
        }

        await _upsert_feature_snapshot(conn, {
            "competition_season_id": season_id,
            "match_id": match_id,
            "team_id": team_id,
            "team_side": side,
            "feature_set_version": FEATURE_SET_VERSION,
            "as_of": as_of,
            **critical,
            "features": json.dumps(secondary),
            "source_hash": sha256_json({**critical, **secondary}),
        })

        results[side.lower()] = critical

    return results


async def get_feature_snapshot(
    conn: AsyncConnection,
    match_id: str,
    team_id: str,
    team_side: str,
) -> dict[str, Any] | None:
    """Read materialized feature snapshot. Called by prediction pipeline — no recalculation."""
    row = await conn.execute(
        text("""
            SELECT *
            FROM feature_snapshots
            WHERE match_id = cast(:match_id as uuid)
              AND team_id  = cast(:team_id as uuid)
              AND team_side = :team_side
              AND feature_set_version = :version
            ORDER BY as_of DESC
            LIMIT 1
        """),
        {
            "match_id": match_id,
            "team_id": team_id,
            "team_side": team_side,
            "version": FEATURE_SET_VERSION,
        },
    )
    r = row.fetchone()
    return dict(r._mapping) if r else None


async def get_matches_needing_snapshots(
    conn: AsyncConnection,
    days_ahead: int = 14,
    days_behind: int = 1,
) -> list[str]:
    """Return match_ids that need feature snapshot building or refresh."""
    rows = await conn.execute(
        text("""
            SELECT DISTINCT m.match_id::text
            FROM matches m
            WHERE m.kickoff_at BETWEEN now() - make_interval(days => :days_behind)
                                    AND now() + make_interval(days => :days_ahead)
              AND m.status IN ('SCHEDULED', 'LIVE', 'FINISHED')
              AND NOT EXISTS (
                SELECT 1 FROM feature_snapshots fs
                WHERE fs.match_id = m.match_id
                  AND fs.feature_set_version = :version
                  AND fs.team_side IS NOT NULL
              )
            ORDER BY m.kickoff_at ASC
        """),
        {"days_behind": days_behind, "days_ahead": days_ahead, "version": FEATURE_SET_VERSION},
    )
    return [r[0] for r in rows]

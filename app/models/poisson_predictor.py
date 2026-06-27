"""
Poisson predictor — Phase 1.

Reads ONLY from materialized feature_snapshots (no recalculation at runtime).
Generates 1X2 probabilities using normalized attack/defense strength + ELO diff.
Stores predictions with fixed-contract explanation JSON.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now
from app.features.snapshot_builder import get_feature_snapshot
from app.models.elo_model import elo_expected
from app.models.poisson_model import poisson_1x2

_MODEL_NAME = "poisson_elo_v1"
_MODEL_VERSION = "1.0.0"
_MODEL_FAMILY = "POISSON"
_BASE_GOALS = 1.3      # WC historical average goals per team per game
_HOME_ADVANTAGE = 1.10  # multiplicative factor for home team when not neutral

# Stage pressure adjustments to home advantage
_STAGE_HOME_ADV: dict[str, float] = {
    "GROUP_STAGE": 1.10,
    "ROUND_OF_16": 1.05,
    "QUARTER_FINAL": 1.03,
    "SEMI_FINAL": 1.02,
    "FINAL": 1.0,
    "THIRD_PLACE": 1.0,
}


async def _get_or_create_model_registry(conn: AsyncConnection) -> str:
    row = await conn.execute(
        text("""
            SELECT model_id::text FROM model_registry
            WHERE model_name = :name AND model_version = :version
            LIMIT 1
        """),
        {"name": _MODEL_NAME, "version": _MODEL_VERSION},
    )
    r = row.fetchone()
    if r:
        return r[0]

    row = await conn.execute(
        text("""
            INSERT INTO model_registry (model_name, model_version, model_family, champion_status)
            VALUES (:name, :version, :family, 'CHAMPION')
            ON CONFLICT (model_name, model_version) DO UPDATE
              SET champion_status = 'CHAMPION'
            RETURNING model_id::text
        """),
        {"name": _MODEL_NAME, "version": _MODEL_VERSION, "family": _MODEL_FAMILY},
    )
    return row.fetchone()[0]


async def _get_competition_avg_goals(
    conn: AsyncConnection, competition_season_id: str, before: Any
) -> float:
    row = await conn.execute(
        text("""
            SELECT avg((mp.score + opp.score)::numeric) AS avg_total
            FROM matches m
            JOIN match_participants mp ON mp.match_id = m.match_id AND mp.side = 'HOME'
            JOIN match_participants opp ON opp.match_id = m.match_id AND opp.side = 'AWAY'
            WHERE m.competition_season_id = cast(:season_id as uuid)
              AND m.status = 'FINISHED'
              AND m.kickoff_at < :before
              AND mp.score IS NOT NULL AND opp.score IS NOT NULL
        """),
        {"season_id": competition_season_id, "before": before},
    )
    r = row.fetchone()
    if r and r[0] is not None:
        avg_total = float(r[0])
        return avg_total / 2.0  # per team per game
    return _BASE_GOALS


async def _get_market_and_selections(
    conn: AsyncConnection,
) -> tuple[str, dict[str, str]]:
    """Return (market_id, {selection_code: selection_id}) for the 1X2 market."""
    row = await conn.execute(
        text("SELECT market_id::text FROM markets WHERE market_code = '1X2' LIMIT 1")
    )
    r = row.fetchone()
    if not r:
        raise RuntimeError("1X2 market not found in markets table")
    market_id = r[0]

    rows = await conn.execute(
        text("""
            SELECT selection_code, selection_id::text
            FROM market_selections
            WHERE market_id = cast(:market_id as uuid)
              AND selection_code IN ('HOME', 'DRAW', 'AWAY')
        """),
        {"market_id": market_id},
    )
    selections = {r2[0]: r2[1] for r2 in rows}
    return market_id, selections


async def _upsert_prediction(
    conn: AsyncConnection,
    model_run_id: str,
    feature_snapshot_id: str,
    competition_season_id: str,
    match_id: str,
    market_id: str,
    selection_id: str,
    raw_probability: float,
    fair_odds: float,
    explanation: dict,
    as_of: Any,
) -> str:
    row = await conn.execute(
        text("""
            INSERT INTO model_predictions (
              model_run_id, feature_snapshot_id, competition_season_id, match_id,
              market_id, selection_id, raw_probability, calibrated_probability,
              fair_odds, prediction_status, explanation, as_of, flags, payload
            )
            VALUES (
              cast(:model_run_id as uuid),
              cast(:feature_snapshot_id as uuid),
              cast(:competition_season_id as uuid),
              cast(:match_id as uuid),
              cast(:market_id as uuid),
              cast(:selection_id as uuid),
              :raw_probability,
              NULL,
              :fair_odds,
              'RAW_ONLY',
              cast(:explanation as jsonb),
              :as_of,
              '{}',
              cast(:payload as jsonb)
            )
            ON CONFLICT (model_run_id, match_id, market_id, selection_id, line, as_of)
            DO UPDATE SET
              raw_probability = excluded.raw_probability,
              fair_odds       = excluded.fair_odds,
              explanation     = excluded.explanation,
              as_of           = excluded.as_of
            RETURNING prediction_id::text
        """),
        {
            "model_run_id": model_run_id,
            "feature_snapshot_id": feature_snapshot_id,
            "competition_season_id": competition_season_id,
            "match_id": match_id,
            "market_id": market_id,
            "selection_id": selection_id,
            "raw_probability": round(raw_probability, 6),
            "fair_odds": round(fair_odds, 4),
            "explanation": json.dumps(explanation, cls=_DecimalEncoder),
            "payload": json.dumps({}),
            "as_of": as_of,
        },
    )
    return row.fetchone()[0]


async def run_poisson_prediction(
    conn: AsyncConnection,
    match_id: str,
    home_team_id: str,
    away_team_id: str,
    competition_season_id: str,
    model_run_id: str,
) -> dict[str, Any]:
    """
    Generate Poisson 1X2 predictions for a match.
    Reads feature snapshots — no feature recalculation.
    """
    as_of = utc_now()

    home_fs = await get_feature_snapshot(conn, match_id, home_team_id, "HOME")
    away_fs = await get_feature_snapshot(conn, match_id, away_team_id, "AWAY")

    if not home_fs or not away_fs:
        return {
            "error": "missing_feature_snapshots",
            "match_id": match_id,
            "home_snapshot": bool(home_fs),
            "away_snapshot": bool(away_fs),
        }

    market_id, selections = await _get_market_and_selections(conn)

    # Competition avg goals (home team perspective kicks off window)
    comp_avg = await _get_competition_avg_goals(conn, competition_season_id, before=as_of)

    # Home advantage (neutral venue cancels it)
    is_neutral = bool(home_fs.get("is_neutral"))
    stage_type = home_fs.get("stage_type") or "GROUP_STAGE"
    base_home_adv = 1.0 if is_neutral else _STAGE_HOME_ADV.get(stage_type, _HOME_ADVANTAGE)

    home_atk = float(home_fs.get("attack_strength") or 1.0)
    home_def = float(home_fs.get("defense_strength") or 1.0)
    away_atk = float(away_fs.get("attack_strength") or 1.0)
    away_def = float(away_fs.get("defense_strength") or 1.0)

    home_lambda = comp_avg * home_atk * away_def * base_home_adv
    away_lambda = comp_avg * away_atk * home_def

    home_lambda = max(home_lambda, 0.05)
    away_lambda = max(away_lambda, 0.05)

    probs = poisson_1x2(home_lambda, away_lambda, max_goals=10)

    elo_diff = float(home_fs.get("elo_diff") or 0.0)
    elo_g = float(home_fs.get("elo_global") or 1500.0)
    opp_g = elo_g - elo_diff

    explanation: dict[str, Any] = {
        "model_family": _MODEL_FAMILY,
        "model_version": _MODEL_VERSION,
        "feature_set_version": home_fs.get("feature_set_version", "v1"),
        "feature_completeness": float(home_fs.get("feature_completeness") or 0),
        "lambda_components": {
            "base_goals": round(comp_avg, 4),
            "home_attack_strength": round(home_atk, 4),
            "away_defense_strength": round(away_def, 4),
            "home_advantage_factor": round(base_home_adv, 4),
            "home_lambda": round(home_lambda, 4),
            "away_attack_strength": round(away_atk, 4),
            "home_defense_strength": round(home_def, 4),
            "away_lambda": round(away_lambda, 4),
        },
        "feature_contributions": [
            {"feature": "elo_diff",         "value": round(elo_diff, 2), "direction": "HOME" if elo_diff > 0 else "AWAY"},
            {"feature": "attack_strength",  "value": round(home_atk, 4), "direction": "HOME"},
            {"feature": "defense_strength", "value": round(away_def, 4), "direction": "HOME"},
            {"feature": "rest_days",        "value": home_fs.get("rest_days"), "direction": "NEUTRAL"},
        ],
        "confidence_factors": [
            {"factor": "elo_data_available",   "ok": home_fs.get("elo_global") is not None},
            {"factor": "form_data_available",  "ok": (home_fs.get("feature_completeness") or 0) > 0.3},
            {"factor": "calibration_available", "ok": False},
            {"factor": "odds_available",        "ok": False},
            {"factor": "lineup_available",      "ok": False},
        ],
        "warnings": (["neutral_venue_no_home_advantage"] if is_neutral else []),
    }

    home_fs_id = str(home_fs["feature_snapshot_id"])
    stored: dict[str, str] = {}

    for sel_code, raw_prob in probs.items():
        sel_id = selections.get(sel_code)
        if not sel_id:
            continue
        fair_odds = 1.0 / raw_prob if raw_prob > 0 else 99.0
        pred_id = await _upsert_prediction(
            conn,
            model_run_id=model_run_id,
            feature_snapshot_id=home_fs_id,
            competition_season_id=competition_season_id,
            match_id=match_id,
            market_id=market_id,
            selection_id=sel_id,
            raw_probability=raw_prob,
            fair_odds=fair_odds,
            explanation=explanation,
            as_of=as_of,
        )
        stored[sel_code] = pred_id

    return {
        "match_id": match_id,
        "home_lambda": round(home_lambda, 4),
        "away_lambda": round(away_lambda, 4),
        "probabilities": {k: round(v, 4) for k, v in probs.items()},
        "predictions": stored,
    }

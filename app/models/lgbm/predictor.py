"""
LightGBM predictor — Phase 3.

Loads CHAMPION LightGBM model from model_registry and runs inference
on upcoming matches. Reads ONLY from feature_snapshots (no recalculation).
Updates model_predictions with lgbm raw_probability alongside Poisson.
"""
from __future__ import annotations

import json
import pickle
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now
from app.features.snapshot_builder import get_feature_snapshot
from app.models.lgbm.trainer import FEATURE_COLS, predict_lgbm

_MODEL_NAME = "lgbm_v1"
_MODEL_VERSION = "1.0.0"
_MODEL_FAMILY = "LGBM"


async def _load_champion_lgbm(conn: AsyncConnection) -> tuple[str, bytes] | None:
    """Load champion LightGBM model bytes from model_registry payload."""
    row = await conn.execute(
        text("""
            SELECT model_id::text, payload
            FROM model_registry
            WHERE model_name = :name
              AND champion_status = 'CHAMPION'
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"name": _MODEL_NAME},
    )
    r = row.fetchone()
    if not r:
        return None
    model_id = r[0]
    payload = r[1] if isinstance(r[1], dict) else json.loads(r[1])
    model_hex = payload.get("model_hex")
    if not model_hex:
        return None
    return model_id, bytes.fromhex(model_hex)


async def run_lgbm_prediction(
    conn: AsyncConnection,
    match_id: str,
    home_team_id: str,
    away_team_id: str,
    competition_season_id: str,
    model_run_id: str,
) -> dict[str, Any]:
    champion = await _load_champion_lgbm(conn)
    if not champion:
        return {"error": "no_champion_lgbm_model", "match_id": match_id}

    model_id, model_bytes = champion

    home_fs = await get_feature_snapshot(conn, match_id, home_team_id, "HOME")
    away_fs = await get_feature_snapshot(conn, match_id, away_team_id, "AWAY")

    if not home_fs or not away_fs:
        return {
            "error": "missing_feature_snapshots",
            "match_id": match_id,
            "home_snapshot": bool(home_fs),
            "away_snapshot": bool(away_fs),
        }

    feature_row = {
        "home_elo_global": home_fs.get("elo_global"),
        "home_elo_diff": home_fs.get("elo_diff"),
        "home_attack_strength": home_fs.get("attack_strength"),
        "home_defense_strength": home_fs.get("defense_strength"),
        "home_form_points": home_fs.get("form_points"),
        "home_form_gd": home_fs.get("form_gd"),
        "home_rest_days": home_fs.get("rest_days"),
        "away_elo_global": away_fs.get("elo_global"),
        "away_attack_strength": away_fs.get("attack_strength"),
        "away_defense_strength": away_fs.get("defense_strength"),
        "away_form_points": away_fs.get("form_points"),
        "away_form_gd": away_fs.get("form_gd"),
        "away_rest_days": away_fs.get("rest_days"),
        "stage_pressure": home_fs.get("stage_pressure"),
        "is_neutral": 1.0 if home_fs.get("is_neutral") else 0.0,
        "feature_completeness": home_fs.get("feature_completeness"),
    }

    home_win_prob = predict_lgbm(model_bytes, feature_row)
    # Naive complement split for DRAW and AWAY (to be replaced with multi-class in v2)
    remaining = 1.0 - home_win_prob
    draw_prob = round(remaining * 0.40, 6)
    away_prob = round(remaining * 0.60, 6)

    probs = {"HOME": home_win_prob, "DRAW": draw_prob, "AWAY": away_prob}

    market_row = await conn.execute(
        text("SELECT market_id::text FROM markets WHERE market_code = '1X2' LIMIT 1")
    )
    mr = market_row.fetchone()
    if not mr:
        return {"error": "no_1x2_market", "match_id": match_id}
    market_id = mr[0]

    sel_rows = await conn.execute(
        text("""
            SELECT selection_code, selection_id::text
            FROM market_selections
            WHERE market_id = cast(:market_id as uuid)
              AND selection_code IN ('HOME', 'DRAW', 'AWAY')
        """),
        {"market_id": market_id},
    )
    selections = {r[0]: r[1] for r in sel_rows}

    as_of = utc_now()
    home_fs_id = str(home_fs["feature_snapshot_id"])

    explanation = {
        "model_family": _MODEL_FAMILY,
        "model_version": _MODEL_VERSION,
        "feature_set_version": home_fs.get("feature_set_version", "v1"),
        "feature_completeness": home_fs.get("feature_completeness"),
        "feature_contributions": [
            {"feature": k, "value": feature_row.get(k)} for k in FEATURE_COLS
        ],
        "warnings": [],
    }

    stored: dict[str, str] = {}
    for sel_code, raw_prob in probs.items():
        sel_id = selections.get(sel_code)
        if not sel_id:
            continue
        fair_odds = round(1.0 / raw_prob, 4) if raw_prob > 0 else 99.0
        row = await conn.execute(
            text("""
                INSERT INTO model_predictions (
                  model_run_id, feature_snapshot_id, competition_season_id, match_id,
                  market_id, selection_id, raw_probability, calibrated_probability,
                  fair_odds, prediction_status, explanation, as_of, flags, payload
                )
                VALUES (
                  cast(:model_run_id as uuid), cast(:fs_id as uuid),
                  cast(:season_id as uuid), cast(:match_id as uuid),
                  cast(:market_id as uuid), cast(:sel_id as uuid),
                  :raw_probability, NULL, :fair_odds,
                  'RAW_ONLY', cast(:explanation as jsonb), :as_of, '{}', cast(:payload as jsonb)
                )
                ON CONFLICT (model_run_id, match_id, market_id, selection_id)
                DO UPDATE SET
                  raw_probability = excluded.raw_probability,
                  fair_odds       = excluded.fair_odds,
                  explanation     = excluded.explanation,
                  as_of           = excluded.as_of
                RETURNING prediction_id::text
            """),
            {
                "model_run_id": model_run_id,
                "fs_id": home_fs_id,
                "season_id": competition_season_id,
                "match_id": match_id,
                "market_id": market_id,
                "sel_id": sel_id,
                "raw_probability": raw_prob,
                "fair_odds": fair_odds,
                "explanation": json.dumps(explanation),
                "payload": json.dumps({}),
                "as_of": as_of,
            },
        )
        stored[sel_code] = row.fetchone()[0]

    return {
        "match_id": match_id,
        "model": _MODEL_NAME,
        "probabilities": probs,
        "predictions": stored,
    }

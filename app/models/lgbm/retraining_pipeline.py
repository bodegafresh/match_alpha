"""
LightGBM retraining pipeline — Phase 3.

Orchestrates: load dataset → train → evaluate → register as CHALLENGER →
optionally promote to CHAMPION via promotion_rules.

This is an OFFLINE operation — intended to be triggered manually or by CI,
not during the daily API job cycle.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.calibration.evaluator import run_calibration
from app.core.time import utc_now
from app.feedback.calibration_service import calibration_metrics
from app.models.lgbm.trainer import FEATURE_COLS, train_lgbm
from app.models.promotion_rules import should_promote_challenger
from app.training.dataset_builder import build_dataset

_MODEL_NAME = "lgbm_v1"
_MODEL_FAMILY = "LGBM"


async def _get_champion_metrics(conn: AsyncConnection, model_name: str) -> dict | None:
    row = await conn.execute(
        text("""
            SELECT mr.model_id::text, cr.brier_score, cr.log_loss, cr.ece
            FROM model_registry mr
            LEFT JOIN calibration_runs cr ON cr.model_id = mr.model_id
            WHERE mr.model_name = :name AND mr.champion_status = 'CHAMPION'
            ORDER BY cr.created_at DESC NULLS LAST
            LIMIT 1
        """),
        {"name": model_name},
    )
    r = row.fetchone()
    if not r:
        return None
    return {"model_id": r[0], "brier_score": r[1], "log_loss": r[2], "ece": r[3]}


async def run_retraining(
    conn: AsyncConnection,
    competition_season_id: str,
    auto_promote: bool = False,
) -> dict[str, Any]:
    """
    Full retraining pipeline. Returns result dict.
    auto_promote=True → promotes to CHAMPION if promotion rules pass.
    """
    # Get champion model for baseline metrics
    champion = await _get_champion_metrics(conn, _MODEL_NAME)

    # Get current champion model_id (Poisson or existing LGBM)
    any_champion_row = await conn.execute(
        text("""
            SELECT model_id::text FROM model_registry
            WHERE champion_status = 'CHAMPION'
            ORDER BY created_at DESC LIMIT 1
        """)
    )
    ac = any_champion_row.fetchone()
    champion_model_id = ac[0] if ac else None

    if not champion_model_id:
        return {"status": "WARN", "reason": "no_champion_model_for_dataset"}

    # Build dataset
    rows = await build_dataset(conn, champion_model_id, competition_season_id)
    n = len(rows)
    if n < 200:
        return {"status": "WARN", "reason": "insufficient_samples", "n": n, "required": 200}

    # Train
    train_result = train_lgbm(rows)
    if not train_result.get("ok"):
        return {"status": "WARN", "reason": train_result.get("reason"), "n": n}

    model_bytes = train_result.pop("model_bytes")

    # Determine next version
    version_row = await conn.execute(
        text("""
            SELECT model_version FROM model_registry
            WHERE model_name = :name ORDER BY created_at DESC LIMIT 1
        """),
        {"name": _MODEL_NAME},
    )
    vr = version_row.fetchone()
    if vr:
        try:
            major, minor = vr[0].split(".")
            new_version = f"{major}.{int(minor) + 1}.0"
        except Exception:
            new_version = "1.0.0"
    else:
        new_version = "1.0.0"

    # Register as CHALLENGER with model bytes stored as hex in payload
    payload = {
        **train_result,
        "model_hex": model_bytes.hex(),
        "feature_cols": FEATURE_COLS,
        "trained_at": utc_now().isoformat(),
        "n_total": n,
    }

    reg_row = await conn.execute(
        text("""
            INSERT INTO model_registry (model_name, model_version, model_family, champion_status, payload)
            VALUES (:name, :version, :family, 'CHALLENGER', cast(:payload as jsonb))
            ON CONFLICT (model_name, model_version) DO UPDATE
              SET champion_status = 'CHALLENGER', payload = excluded.payload
            RETURNING model_id::text
        """),
        {
            "name": _MODEL_NAME,
            "version": new_version,
            "family": _MODEL_FAMILY,
            "payload": json.dumps(payload),
        },
    )
    challenger_model_id = reg_row.fetchone()[0]

    result = {
        "status": "OK",
        "challenger_model_id": challenger_model_id,
        "version": new_version,
        "n_total": n,
        "n_train": train_result.get("n_train"),
        "n_val": train_result.get("n_val"),
        "challenger_brier": train_result.get("brier_score"),
        "challenger_log_loss": train_result.get("log_loss"),
        "promoted": False,
    }

    if not auto_promote:
        return result

    # Evaluate promotion
    decision = should_promote_challenger(
        champion_brier=champion["brier_score"] if champion else None,
        challenger_brier=train_result.get("brier_score"),
        champion_log_loss=champion["log_loss"] if champion else None,
        challenger_log_loss=train_result.get("log_loss"),
        challenger_ece=None,  # calibration runs after promotion
        max_ece=0.10,
        sample_size=n,
        min_sample_size=200,
        severe_drift_open=False,
    )

    if decision.promote:
        # Archive current champion
        if champion:
            await conn.execute(
                text("""
                    UPDATE model_registry SET champion_status = 'ARCHIVED'
                    WHERE model_id = cast(:model_id as uuid)
                """),
                {"model_id": champion["model_id"]},
            )
        # Promote challenger
        await conn.execute(
            text("""
                UPDATE model_registry SET champion_status = 'CHAMPION'
                WHERE model_id = cast(:model_id as uuid)
            """),
            {"model_id": challenger_model_id},
        )
        result["promoted"] = True
        result["promotion_reasons"] = []
    else:
        result["promotion_reasons"] = decision.reasons

    return result

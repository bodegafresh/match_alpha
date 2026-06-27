"""
Calibration evaluator — Phase 2.

Orchestrates: load settled predictions → fit calibration →
persist calibration_run + calibration_bins → update model_predictions.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.calibration.fitter import apply_calibration_map, fit_isotonic, fit_platt
from app.core.time import utc_now
from app.feedback.calibration_service import calibration_metrics

MIN_SAMPLES = 30


async def load_settled_predictions(
    conn: AsyncConnection,
    model_id: str,
    market_code: str = "1X2",
) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text("""
            SELECT
              mp.prediction_id::text,
              mp.raw_probability,
              ms.selection_code,
              m.home_score,
              m.away_score
            FROM model_predictions mp
            JOIN model_runs mr ON mr.model_run_id = mp.model_run_id
            JOIN markets mk ON mk.market_id = mp.market_id
            JOIN market_selections ms ON ms.selection_id = mp.selection_id
            JOIN matches m ON m.match_id = mp.match_id
            JOIN betting_decisions bd ON bd.prediction_id = mp.prediction_id
            WHERE mr.model_id = cast(:model_id as uuid)
              AND mk.market_code = :market_code
              AND bd.settlement_status = 'SETTLED'
              AND m.home_score IS NOT NULL
              AND m.away_score IS NOT NULL
              AND mp.raw_probability IS NOT NULL
        """),
        {"model_id": model_id, "market_code": market_code},
    )

    results = []
    for r in rows:
        d = dict(r._mapping)
        sel = d["selection_code"]
        hs = int(d["home_score"])
        aws = int(d["away_score"])
        if sel == "HOME":
            outcome = 1 if hs > aws else 0
        elif sel == "AWAY":
            outcome = 1 if aws > hs else 0
        elif sel == "DRAW":
            outcome = 1 if hs == aws else 0
        else:
            continue
        results.append({
            "prediction_id": d["prediction_id"],
            "raw_probability": float(d["raw_probability"]),
            "outcome": outcome,
        })

    return results


async def run_calibration(
    conn: AsyncConnection,
    model_id: str,
    competition_season_id: str,
    method: str = "ISOTONIC",
) -> dict[str, Any]:
    predictions = await load_settled_predictions(conn, model_id)
    n = len(predictions)

    if n < MIN_SAMPLES:
        return {"status": "WARN", "reason": "insufficient_samples", "n": n}

    raw_probs = [p["raw_probability"] for p in predictions]
    outcomes = [p["outcome"] for p in predictions]

    if method == "PLATT":
        fit_result = fit_platt(raw_probs, outcomes)
    else:
        fit_result = fit_isotonic(raw_probs, outcomes)

    if not fit_result.get("ok"):
        return {"status": "WARN", "reason": fit_result.get("reason"), "n": n}

    cal_map = fit_result["calibration_map"]
    calibrated_probs = [apply_calibration_map(p, cal_map) for p in raw_probs]

    metrics = calibration_metrics(calibrated_probs, outcomes)

    market_row = await conn.execute(
        text("SELECT market_id::text FROM markets WHERE market_code = '1X2' LIMIT 1")
    )
    mr = market_row.fetchone()
    market_id = mr[0] if mr else None

    run_row = await conn.execute(
        text("""
            INSERT INTO calibration_runs (
              model_id, competition_season_id, market_id, method,
              sample_size, brier_score, log_loss, ece, sharpness,
              train_start_at, train_end_at, payload
            )
            VALUES (
              cast(:model_id as uuid),
              cast(:season_id as uuid),
              cast(:market_id as uuid),
              cast(:method as calibration_method),
              :sample_size, :brier, :log_loss, :ece, :sharpness,
              :now, :now,
              cast(:payload as jsonb)
            )
            RETURNING calibration_run_id::text
        """),
        {
            "model_id": model_id,
            "season_id": competition_season_id,
            "market_id": market_id,
            "method": method,
            "sample_size": n,
            "brier": round(metrics.brier_score, 6),
            "log_loss": round(metrics.log_loss, 6),
            "ece": round(metrics.ece, 6),
            "sharpness": round(metrics.sharpness, 6),
            "now": utc_now(),
            "payload": json.dumps(fit_result),
        },
    )
    calibration_run_id = run_row.fetchone()[0]

    # Insert calibration bins (10 bins)
    bins = 10
    for i in range(bins):
        low = i / bins
        high = (i + 1) / bins
        bucket = [(r, o) for r, o in zip(raw_probs, outcomes) if low <= r < high or (i == bins - 1 and r == 1.0)]
        if not bucket:
            continue
        pred_mean = sum(r for r, _ in bucket) / len(bucket)
        obs_rate = sum(o for _, o in bucket) / len(bucket)
        await conn.execute(
            text("""
                INSERT INTO calibration_bins (
                  calibration_run_id, bin_lower, bin_upper,
                  predicted_mean, observed_rate, sample_size
                )
                VALUES (
                  cast(:run_id as uuid), :low, :high,
                  :pred_mean, :obs_rate, :sample_size
                )
            """),
            {
                "run_id": calibration_run_id,
                "low": low,
                "high": high,
                "pred_mean": round(pred_mean, 6),
                "obs_rate": round(obs_rate, 6),
                "sample_size": len(bucket),
            },
        )

    # Bulk-update model_predictions with calibrated probabilities
    updated = 0
    for pred, cal_prob in zip(predictions, calibrated_probs):
        await conn.execute(
            text("""
                UPDATE model_predictions
                SET calibrated_probability = :cal_prob,
                    prediction_status = 'CALIBRATED'
                WHERE prediction_id = cast(:prediction_id as uuid)
            """),
            {"cal_prob": cal_prob, "prediction_id": pred["prediction_id"]},
        )
        updated += 1

    return {
        "status": "OK",
        "calibration_run_id": calibration_run_id,
        "method": method,
        "n": n,
        "updated": updated,
        "ece": round(metrics.ece, 6),
        "brier_score": round(metrics.brier_score, 6),
    }

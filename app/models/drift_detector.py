"""
Drift detector — Phase 3.

Detects distribution shift in model predictions vs settled outcomes.
Uses Population Stability Index (PSI) on raw_probability distributions
and compares recent Brier score vs historical baseline.

Severity:
  INFO     → PSI < 0.1 or Brier delta < 0.01
  WARN     → PSI 0.1–0.2 or Brier delta 0.01–0.03
  ERROR    → PSI > 0.2 or Brier delta > 0.03
  CRITICAL → PSI > 0.25 or Brier delta > 0.05
"""
from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now

_BINS = 10
_PSI_WARN = 0.10
_PSI_ERROR = 0.20
_PSI_CRITICAL = 0.25
_BRIER_WARN = 0.01
_BRIER_ERROR = 0.03
_BRIER_CRITICAL = 0.05
_MIN_SAMPLES = 30


def _psi(expected: list[float], actual: list[float], bins: int = _BINS) -> float:
    """Population Stability Index between two probability distributions."""
    eps = 1e-6
    edges = [i / bins for i in range(bins + 1)]

    def bucket_fracs(probs: list[float]) -> list[float]:
        counts = [0] * bins
        for p in probs:
            idx = min(int(p * bins), bins - 1)
            counts[idx] += 1
        n = len(probs) or 1
        return [max(c / n, eps) for c in counts]

    exp_f = bucket_fracs(expected)
    act_f = bucket_fracs(actual)
    return sum((a - e) * math.log(a / e) for e, a in zip(exp_f, act_f, strict=True))


def _brier(probs: list[float], outcomes: list[int]) -> float:
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes, strict=True)) / len(probs)


def _severity(psi: float, brier_delta: float) -> str:
    if psi >= _PSI_CRITICAL or brier_delta >= _BRIER_CRITICAL:
        return "CRITICAL"
    if psi >= _PSI_ERROR or brier_delta >= _BRIER_ERROR:
        return "ERROR"
    if psi >= _PSI_WARN or brier_delta >= _BRIER_WARN:
        return "WARN"
    return "INFO"


async def detect_drift(
    conn: AsyncConnection,
    model_id: str,
    competition_season_id: str,
    recent_days: int = 30,
    baseline_days: int = 90,
) -> dict[str, Any]:
    """
    Compare recent prediction distribution vs historical baseline.
    Persists a drift_report row. Returns the drift analysis.
    """
    now = utc_now()

    async def _load(after, before) -> list[dict]:
        rows = await conn.execute(
            text("""
                SELECT mp.raw_probability, ms.selection_code, m.home_score, m.away_score
                FROM model_predictions mp
                JOIN model_runs mr ON mr.model_run_id = mp.model_run_id
                JOIN market_selections ms ON ms.selection_id = mp.selection_id
                JOIN matches m ON m.match_id = mp.match_id
                JOIN betting_decisions bd ON bd.prediction_id = mp.prediction_id
                WHERE mr.model_id = cast(:model_id as uuid)
                  AND m.kickoff_at >= :after
                  AND m.kickoff_at < :before
                  AND bd.settlement_status = 'SETTLED'
                  AND mp.raw_probability IS NOT NULL
            """),
            {"model_id": model_id, "after": after, "before": before},
        )
        return [dict(r._mapping) for r in rows]

    from datetime import timedelta
    recent_after = now - timedelta(days=recent_days)
    baseline_after = now - timedelta(days=baseline_days + recent_days)
    baseline_before = recent_after

    recent_data = await _load(recent_after, now)
    baseline_data = await _load(baseline_after, baseline_before)

    if len(recent_data) < _MIN_SAMPLES or len(baseline_data) < _MIN_SAMPLES:
        return {
            "status": "WARN",
            "reason": "insufficient_samples",
            "n_recent": len(recent_data),
            "n_baseline": len(baseline_data),
            "required": _MIN_SAMPLES,
        }

    def outcome_for(d: dict) -> int:
        sel = d["selection_code"]
        hs = int(d["home_score"])
        aws = int(d["away_score"])
        if sel == "HOME":
            return 1 if hs > aws else 0
        if sel == "AWAY":
            return 1 if aws > hs else 0
        return 1 if hs == aws else 0

    recent_probs = [float(d["raw_probability"]) for d in recent_data]
    recent_outcomes = [outcome_for(d) for d in recent_data]
    baseline_probs = [float(d["raw_probability"]) for d in baseline_data]
    baseline_outcomes = [outcome_for(d) for d in baseline_data]

    psi_score = round(_psi(baseline_probs, recent_probs), 6)
    recent_brier = round(_brier(recent_probs, recent_outcomes), 6)
    baseline_brier = round(_brier(baseline_probs, baseline_outcomes), 6)
    brier_delta = round(abs(recent_brier - baseline_brier), 6)
    severity = _severity(psi_score, brier_delta)

    payload = {
        "psi": psi_score,
        "recent_brier": recent_brier,
        "baseline_brier": baseline_brier,
        "brier_delta": brier_delta,
        "n_recent": len(recent_data),
        "n_baseline": len(baseline_data),
        "recent_days": recent_days,
        "baseline_days": baseline_days,
    }

    await conn.execute(
        text("""
            INSERT INTO drift_reports (
              competition_season_id, model_id, feature_set_version,
              drift_score, severity, detected_at, payload
            )
            VALUES (
              cast(:season_id as uuid), cast(:model_id as uuid),
              'v1', :drift_score, cast(:severity as severity_level),
              :now, cast(:payload as jsonb)
            )
        """),
        {
            "season_id": competition_season_id,
            "model_id": model_id,
            "drift_score": psi_score,
            "severity": severity,
            "now": now,
            "payload": json.dumps(payload),
        },
    )

    return {
        "status": "OK",
        "severity": severity,
        **payload,
    }

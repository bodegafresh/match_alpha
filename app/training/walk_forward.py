"""
Walk-forward backtesting — Phase 2.

Uses ONLY historical feature_snapshots (as_of < kickoff_at).
NEVER recalculates features for historical matches.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now

MIN_SETTLED = 30


async def run_walk_forward(
    conn: AsyncConnection,
    model_id: str,
    competition_season_id: str,
    window_days: int = 90,
    test_days: int = 30,
) -> dict[str, Any]:
    # Get date range of settled predictions
    range_row = await conn.execute(
        text("""
            SELECT
              min(m.kickoff_at) AS min_date,
              max(m.kickoff_at) AS max_date,
              count(*) AS total
            FROM model_predictions mp
            JOIN model_runs mr ON mr.model_run_id = mp.model_run_id
            JOIN betting_decisions bd ON bd.prediction_id = mp.prediction_id
            JOIN matches m ON m.match_id = mp.match_id
            WHERE mr.model_id = cast(:model_id as uuid)
              AND bd.settlement_status = 'SETTLED'
              AND mp.raw_probability IS NOT NULL
        """),
        {"model_id": model_id},
    )
    rng = range_row.fetchone()
    if not rng or not rng[0] or int(rng[2]) < MIN_SETTLED:
        return {
            "status": "WARN",
            "reason": "insufficient_samples",
            "n": int(rng[2]) if rng else 0,
            "windows": [],
            "avg_brier": None,
            "backtest_run_ids": [],
        }

    market_row = await conn.execute(
        text("SELECT market_id::text FROM markets WHERE market_code = '1X2' LIMIT 1")
    )
    mr = market_row.fetchone()
    market_id = mr[0] if mr else None

    min_date = rng[0]
    max_date = rng[1]

    windows = []
    backtest_run_ids = []
    current_train_end = min_date + timedelta(days=window_days)

    while current_train_end < max_date:
        test_end = current_train_end + timedelta(days=test_days)
        window_start = current_train_end - timedelta(days=window_days)

        # Test set: settled predictions in the test window
        test_rows = await conn.execute(
            text("""
                SELECT
                  mp.raw_probability,
                  ms.selection_code,
                  m.home_score,
                  m.away_score
                FROM model_predictions mp
                JOIN model_runs mr2 ON mr2.model_run_id = mp.model_run_id
                JOIN market_selections ms ON ms.selection_id = mp.selection_id
                JOIN feature_snapshots fs ON fs.feature_snapshot_id = mp.feature_snapshot_id
                JOIN matches m ON m.match_id = mp.match_id
                JOIN betting_decisions bd ON bd.prediction_id = mp.prediction_id
                WHERE mr2.model_id = cast(:model_id as uuid)
                  AND m.kickoff_at >= :train_end
                  AND m.kickoff_at < :test_end
                  AND fs.as_of < m.kickoff_at
                  AND bd.settlement_status = 'SETTLED'
                  AND mp.raw_probability IS NOT NULL
            """),
            {"model_id": model_id, "train_end": current_train_end, "test_end": test_end},
        )
        test_data = [dict(r._mapping) for r in test_rows]

        if not test_data:
            current_train_end += timedelta(days=test_days)
            continue

        # Brier score on test set
        brier_sum = 0.0
        for d in test_data:
            sel = d["selection_code"]
            hs = int(d["home_score"])
            aws = int(d["away_score"])
            if sel == "HOME":
                outcome = 1 if hs > aws else 0
            elif sel == "AWAY":
                outcome = 1 if aws > hs else 0
            else:
                outcome = 1 if hs == aws else 0
            p = float(d["raw_probability"])
            brier_sum += (p - outcome) ** 2
        brier = round(brier_sum / len(test_data), 6)

        payload = {
            "brier_score": brier,
            "n_test": len(test_data),
            "window_days": window_days,
            "test_days": test_days,
        }

        run_row = await conn.execute(
            text("""
                INSERT INTO backtest_runs (
                  model_id, competition_season_id, market_id,
                  validation_method, window_start_at, window_end_at, payload
                )
                VALUES (
                  cast(:model_id as uuid),
                  cast(:season_id as uuid),
                  cast(:market_id as uuid),
                  'WALK_FORWARD',
                  :window_start, :window_end,
                  cast(:payload as jsonb)
                )
                RETURNING backtest_run_id::text
            """),
            {
                "model_id": model_id,
                "season_id": competition_season_id,
                "market_id": market_id,
                "window_start": window_start,
                "window_end": test_end,
                "payload": json.dumps(payload),
            },
        )
        backtest_run_id = run_row.fetchone()[0]
        backtest_run_ids.append(backtest_run_id)

        windows.append({
            "window_start": window_start.isoformat(),
            "train_end": current_train_end.isoformat(),
            "test_end": test_end.isoformat(),
            "n_test": len(test_data),
            "brier_score": brier,
            "backtest_run_id": backtest_run_id,
        })

        current_train_end += timedelta(days=test_days)

    avg_brier = round(sum(w["brier_score"] for w in windows) / len(windows), 6) if windows else None

    return {
        "status": "OK",
        "windows": windows,
        "avg_brier": avg_brier,
        "backtest_run_ids": backtest_run_ids,
    }

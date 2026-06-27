"""
Dataset builder — Phase 2.

Builds a labeled dataset from materialized feature_snapshots.
Uses ONLY historical snapshots (fs.as_of < kickoff_at). Never recalculates features.
"""
from __future__ import annotations

import csv
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def build_dataset(
    conn: AsyncConnection,
    model_id: str,
    competition_season_id: str,
    before_date=None,
) -> list[dict[str, Any]]:
    """
    Returns labeled rows: one per settled prediction, with all numeric features
    from the HOME team snapshot + mirrored AWAY features + raw_probability + outcome.
    """
    extra_filter = "AND m.kickoff_at < :before_date" if before_date else ""
    rows = await conn.execute(
        text(f"""
            SELECT
              mp.prediction_id::text,
              mp.raw_probability,
              ms.selection_code,
              m.home_score,
              m.away_score,
              -- Home team features
              home_fs.elo_global          AS home_elo_global,
              home_fs.elo_diff            AS home_elo_diff,
              home_fs.attack_strength     AS home_attack_strength,
              home_fs.defense_strength    AS home_defense_strength,
              home_fs.form_points         AS home_form_points,
              home_fs.form_gd             AS home_form_gd,
              home_fs.rest_days           AS home_rest_days,
              home_fs.stage_pressure      AS stage_pressure,
              home_fs.is_neutral          AS is_neutral,
              home_fs.feature_completeness AS feature_completeness,
              -- Away team features
              away_fs.elo_global          AS away_elo_global,
              away_fs.attack_strength     AS away_attack_strength,
              away_fs.defense_strength    AS away_defense_strength,
              away_fs.form_points         AS away_form_points,
              away_fs.form_gd             AS away_form_gd,
              away_fs.rest_days           AS away_rest_days
            FROM model_predictions mp
            JOIN model_runs mr ON mr.model_run_id = mp.model_run_id
            JOIN markets mk ON mk.market_id = mp.market_id
            JOIN market_selections ms ON ms.selection_id = mp.selection_id
            JOIN matches m ON m.match_id = mp.match_id
            JOIN betting_decisions bd ON bd.prediction_id = mp.prediction_id
            JOIN feature_snapshots home_fs
              ON home_fs.match_id = mp.match_id
             AND home_fs.team_side = 'HOME'
             AND home_fs.as_of < m.kickoff_at
            JOIN feature_snapshots away_fs
              ON away_fs.match_id = mp.match_id
             AND away_fs.team_side = 'AWAY'
             AND away_fs.as_of < m.kickoff_at
            WHERE mr.model_id = cast(:model_id as uuid)
              AND m.competition_season_id = cast(:season_id as uuid)
              AND mk.market_code = '1X2'
              AND ms.selection_code = 'HOME'
              AND bd.settlement_status = 'SETTLED'
              AND m.home_score IS NOT NULL
              AND mp.raw_probability IS NOT NULL
              {extra_filter}
            ORDER BY m.kickoff_at ASC
        """),
        {
            "model_id": model_id,
            "season_id": competition_season_id,
            **({"before_date": before_date} if before_date else {}),
        },
    )

    results = []
    for r in rows:
        d = dict(r._mapping)
        hs = int(d["home_score"])
        aws = int(d["away_score"])
        outcome = 1 if hs > aws else 0
        results.append({
            "prediction_id": d["prediction_id"],
            "raw_probability": float(d["raw_probability"] or 0),
            "outcome": outcome,
            "home_elo_global": float(d["home_elo_global"] or 1500),
            "home_elo_diff": float(d["home_elo_diff"] or 0),
            "home_attack_strength": float(d["home_attack_strength"] or 1),
            "home_defense_strength": float(d["home_defense_strength"] or 1),
            "home_form_points": float(d["home_form_points"] or 0),
            "home_form_gd": float(d["home_form_gd"] or 0),
            "home_rest_days": int(d["home_rest_days"] or 7),
            "away_elo_global": float(d["away_elo_global"] or 1500),
            "away_attack_strength": float(d["away_attack_strength"] or 1),
            "away_defense_strength": float(d["away_defense_strength"] or 1),
            "away_form_points": float(d["away_form_points"] or 0),
            "away_form_gd": float(d["away_form_gd"] or 0),
            "away_rest_days": int(d["away_rest_days"] or 7),
            "stage_pressure": float(d["stage_pressure"] or 0),
            "is_neutral": bool(d["is_neutral"]),
            "feature_completeness": float(d["feature_completeness"] or 0),
        })

    return results


async def build_dataset_to_csv(
    conn: AsyncConnection,
    model_id: str,
    competition_season_id: str,
    output_path: str,
) -> dict[str, Any]:
    rows = await build_dataset(conn, model_id, competition_season_id)
    if not rows:
        return {"status": "WARN", "rows": 0, "output_path": output_path, "reason": "no_data"}

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return {"status": "OK", "rows": len(rows), "output_path": output_path}

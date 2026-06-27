"""
CLV (Closing Line Value) calculator — Phase 2.

CLV = ln(odds_taken / reference_odds)
  > 0 → beat the market (good timing)
  = 0 → matched market
  < 0 → worse than market

Reference priority:
  1. market_closing_odds (CLOSING)
  2. last odds_snapshot before kickoff (LAST_AVAILABLE)
  3. None — cannot compute, skip silently
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def compute_clv(conn: AsyncConnection, betting_decision_id: str) -> dict[str, Any] | None:
    row = await conn.execute(
        text("""
            SELECT
              bd.betting_decision_id::text,
              os.decimal_odds              AS odds_taken,
              bd.match_id::text,
              mp.market_id::text,
              mp.selection_id::text,
              m.kickoff_at
            FROM betting_decisions bd
            JOIN odds_snapshots os ON os.odds_snapshot_id = bd.odds_snapshot_id
            JOIN model_predictions mp ON mp.prediction_id = bd.prediction_id
            JOIN matches m ON m.match_id = bd.match_id
            WHERE bd.betting_decision_id = cast(:decision_id as uuid)
        """),
        {"decision_id": betting_decision_id},
    )
    decision = row.fetchone()
    if not decision:
        return None

    d = dict(decision._mapping)
    odds_taken = float(d["odds_taken"])
    match_id = d["match_id"]
    market_id = d["market_id"]
    selection_id = d["selection_id"]
    kickoff_at = d["kickoff_at"]

    closing_row = await conn.execute(
        text("""
            SELECT closing_decimal_odds FROM market_closing_odds
            WHERE match_id     = cast(:match_id as uuid)
              AND market_id    = cast(:market_id as uuid)
              AND selection_id = cast(:selection_id as uuid)
            LIMIT 1
        """),
        {"match_id": match_id, "market_id": market_id, "selection_id": selection_id},
    )
    closing = closing_row.fetchone()

    if closing:
        reference_odds = float(closing[0])
        clv_source = "CLOSING"
    else:
        last_row = await conn.execute(
            text("""
                SELECT decimal_odds FROM odds_snapshots
                WHERE match_id     = cast(:match_id as uuid)
                  AND market_id    = cast(:market_id as uuid)
                  AND selection_id = cast(:selection_id as uuid)
                  AND captured_at < :kickoff_at
                ORDER BY captured_at DESC
                LIMIT 1
            """),
            {"match_id": match_id, "market_id": market_id, "selection_id": selection_id, "kickoff_at": kickoff_at},
        )
        last = last_row.fetchone()
        if not last:
            return None
        reference_odds = float(last[0])
        clv_source = "LAST_AVAILABLE"

    if reference_odds <= 1.0 or odds_taken <= 1.0:
        return None

    clv_value = round(math.log(odds_taken / reference_odds), 6)

    await conn.execute(
        text("""
            UPDATE betting_decisions
            SET clv_value = :clv_value, clv_source = :clv_source
            WHERE betting_decision_id = cast(:decision_id as uuid)
        """),
        {"clv_value": clv_value, "clv_source": clv_source, "decision_id": betting_decision_id},
    )

    return {
        "betting_decision_id": betting_decision_id,
        "clv_value": clv_value,
        "clv_source": clv_source,
        "odds_taken": odds_taken,
        "reference_odds": reference_odds,
    }


async def compute_pending_clv(conn: AsyncConnection) -> dict[str, Any]:
    rows = await conn.execute(
        text("""
            SELECT betting_decision_id::text
            FROM betting_decisions
            WHERE settlement_status = 'SETTLED'
              AND clv_value IS NULL
        """)
    )
    decision_ids = [r[0] for r in rows]

    computed = 0
    skipped = 0
    errors = 0

    for decision_id in decision_ids:
        try:
            result = await compute_clv(conn, decision_id)
            if result is None:
                skipped += 1
            else:
                computed += 1
        except Exception:
            errors += 1

    return {
        "status": "OK" if errors == 0 else "WARN",
        "computed": computed,
        "skipped_no_reference": skipped,
        "errors": errors,
    }

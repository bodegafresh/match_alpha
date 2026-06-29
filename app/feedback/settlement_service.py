"""
Settlement service — resolves betting decisions against match outcomes.

Uses resolver plugin architecture: one resolver per market_code.
Resolvers auto-register on import via @register_resolver decorator.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Import resolvers to trigger registration
import app.feedback.settlement.resolvers.btts  # noqa: F401
import app.feedback.settlement.resolvers.one_x_two  # noqa: F401
import app.feedback.settlement.resolvers.over_under  # noqa: F401
from app.feedback.settlement.resolver import RESOLVERS
from app.core.time import utc_now


async def _get_pending_decisions(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text("""
            SELECT
              bd.betting_decision_id::text,
              bd.prediction_id::text,
              bd.stake_fraction,
              mp.market_id::text,
              mp.selection_id::text,
              mp.line,
              mk.market_code,
              ms.selection_code,
              m.match_id::text,
              m.home_score,
              m.away_score,
              m.status AS match_status
            FROM betting_decisions bd
            JOIN model_predictions mp ON mp.prediction_id = bd.prediction_id
            JOIN markets mk ON mk.market_id = mp.market_id
            JOIN market_selections ms ON ms.selection_id = mp.selection_id
            JOIN matches m ON m.match_id = bd.match_id
            WHERE bd.settlement_status = 'PENDING'
              AND m.status = 'FINISHED'
              AND m.home_score IS NOT NULL
              AND m.away_score IS NOT NULL
        """)
    )
    return [dict(r._mapping) for r in rows]


async def _settle_decision(
    conn: AsyncConnection,
    decision_id: str,
    outcome: str,
    profit_units: float,
    notes: str,
) -> None:
    await conn.execute(
        text("""
            UPDATE betting_decisions
            SET
              settlement_status        = 'SETTLED',
              settlement_result        = cast(:outcome as text),
              settlement_profit_units  = :profit_units,
              settled_at               = :now
            WHERE betting_decision_id = cast(:decision_id as uuid)
        """),
        {
            "decision_id": decision_id,
            "outcome": outcome,
            "profit_units": profit_units,
            "now": utc_now(),
        },
    )


async def settle_pending_decisions(conn: AsyncConnection) -> dict[str, Any]:
    """
    Resolve all PENDING betting decisions for FINISHED matches.
    Returns summary: {settled, skipped_no_resolver, errors}.
    """
    decisions = await _get_pending_decisions(conn)
    pending_candidates = len(decisions)
    settled = 0
    skipped = 0
    errors = 0

    for d in decisions:
        market_code = d["market_code"]
        resolver = RESOLVERS.get(market_code)

        if not resolver:
            skipped += 1
            continue

        try:
            result = resolver.resolve(
                selection_code=d["selection_code"],
                line=d.get("line"),
                home_score=int(d["home_score"]),
                away_score=int(d["away_score"]),
            )
            await _settle_decision(
                conn,
                decision_id=d["betting_decision_id"],
                outcome=result.outcome,
                profit_units=result.profit_units,
                notes=result.notes,
            )
            settled += 1
        except Exception as exc:
            errors += 1

    return {
        "status": "OK" if errors == 0 else "WARN",
        "pending_candidates": pending_candidates,
        "settled": settled,
        "skipped_no_resolver": skipped,
        "errors": errors,
        "registered_resolvers": list(RESOLVERS.keys()),
    }

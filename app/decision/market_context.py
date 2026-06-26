"""
Market context layer — Phase 1.

Aggregates bookmaker odds data for a match+market into a structured
MarketContext. Used by the decision engine to assess betting eligibility
and downgrade stake sizing.

Rules:
  - NONE liquidity → hard block (NO_ODDS_AVAILABLE)
  - odds_age_minutes > 120 → hard block (ODDS_STALE)
  - LOW liquidity → soft block reason (LOW_LIQUIDITY, reduces stake)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now


@dataclass
class MarketContext:
    match_id: str
    market_id: str
    bookmaker_count: int
    liquidity_tier: str          # 'HIGH' | 'MEDIUM' | 'LOW' | 'NONE'
    market_quality_score: float  # 0.0 – 1.0
    has_odds: bool
    odds_age_minutes: float


def _liquidity_tier(bookmaker_count: int) -> str:
    if bookmaker_count >= 5:
        return "HIGH"
    if bookmaker_count >= 2:
        return "MEDIUM"
    if bookmaker_count >= 1:
        return "LOW"
    return "NONE"


async def build_market_context(
    conn: AsyncConnection,
    match_id: str,
    market_id: str,
) -> MarketContext:
    """
    Build MarketContext from latest odds_snapshots for this match+market.
    Also persists to market_quality_snapshots (append-only via INSERT ON CONFLICT DO NOTHING).
    """
    rows = await conn.execute(
        text("""
            SELECT
              bookmaker_id::text,
              max(captured_at) AS latest_captured_at
            FROM odds_snapshots
            WHERE match_id  = cast(:match_id as uuid)
              AND market_id = cast(:market_id as uuid)
            GROUP BY bookmaker_id
        """),
        {"match_id": match_id, "market_id": market_id},
    )
    snapshots = [dict(r._mapping) for r in rows]

    bookmaker_count = len(snapshots)
    tier = _liquidity_tier(bookmaker_count)
    quality_score = round(min(bookmaker_count / 5.0, 1.0), 4)

    now = utc_now()
    if snapshots:
        latest_ts = max(s["latest_captured_at"] for s in snapshots)
        age_minutes = (now - latest_ts).total_seconds() / 60.0
    else:
        age_minutes = 9999.0

    ctx = MarketContext(
        match_id=match_id,
        market_id=market_id,
        bookmaker_count=bookmaker_count,
        liquidity_tier=tier,
        market_quality_score=quality_score,
        has_odds=bookmaker_count > 0,
        odds_age_minutes=round(age_minutes, 2),
    )

    # Persist snapshot (append-only: ON CONFLICT DO NOTHING)
    await conn.execute(
        text("""
            INSERT INTO market_quality_snapshots (
              match_id, market_id, bookmaker_count,
              liquidity_tier, market_quality_score, snapshot_at
            )
            VALUES (
              cast(:match_id as uuid), cast(:market_id as uuid),
              :bookmaker_count, :liquidity_tier, :market_quality_score, :now
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "match_id": match_id,
            "market_id": market_id,
            "bookmaker_count": bookmaker_count,
            "liquidity_tier": tier,
            "market_quality_score": quality_score,
            "now": now,
        },
    )

    return ctx

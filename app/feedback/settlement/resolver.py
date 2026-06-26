"""
Settlement resolver plugin architecture.

Each market has its own resolver. Add new markets by creating a new class
decorated with @register_resolver — no other changes needed.

Usage:
    from app.feedback.settlement.resolver import RESOLVERS
    resolver = RESOLVERS.get(market_code)
    if resolver:
        result = resolver.resolve(selection_code, line, home_score, away_score)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

RESOLVERS: dict[str, "MarketResolver"] = {}


def register_resolver(cls: type["MarketResolver"]) -> type["MarketResolver"]:
    RESOLVERS[cls.market_code] = cls()
    return cls


@dataclass
class SettlementResult:
    outcome: str       # 'WIN' | 'LOSS' | 'PUSH' | 'VOID'
    profit_units: float
    notes: str = ""


class MarketResolver(ABC):
    market_code: str

    @abstractmethod
    def resolve(
        self,
        selection_code: str,
        line: float | None,
        home_score: int,
        away_score: int,
    ) -> SettlementResult: ...

    def _profit(self, outcome: str, stake_fraction: float, decimal_odds: float) -> float:
        if outcome == "WIN":
            return round(stake_fraction * (decimal_odds - 1), 6)
        if outcome in ("LOSS",):
            return round(-stake_fraction, 6)
        return 0.0

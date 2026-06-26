from app.feedback.settlement.resolver import MarketResolver, SettlementResult, register_resolver


@register_resolver
class BTTSResolver(MarketResolver):
    market_code = "BTTS"

    def resolve(self, selection_code, line, home_score, away_score):
        actual = "YES" if home_score > 0 and away_score > 0 else "NO"
        outcome = "WIN" if selection_code == actual else "LOSS"
        return SettlementResult(outcome=outcome, profit_units=0.0)

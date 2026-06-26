from app.feedback.settlement.resolver import MarketResolver, SettlementResult, register_resolver


@register_resolver
class OneX2Resolver(MarketResolver):
    market_code = "1X2"

    def resolve(self, selection_code, line, home_score, away_score):
        if home_score > away_score:
            actual = "HOME"
        elif away_score > home_score:
            actual = "AWAY"
        else:
            actual = "DRAW"
        outcome = "WIN" if selection_code == actual else "LOSS"
        return SettlementResult(outcome=outcome, profit_units=0.0)

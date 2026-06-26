from app.feedback.settlement.resolver import MarketResolver, SettlementResult, register_resolver


@register_resolver
class OverUnderResolver(MarketResolver):
    market_code = "OVER_UNDER"

    def resolve(self, selection_code, line, home_score, away_score):
        if line is None:
            return SettlementResult(outcome="VOID", profit_units=0.0, notes="missing_line")
        total = home_score + away_score
        if total == line:
            return SettlementResult(outcome="PUSH", profit_units=0.0)
        actual = "OVER" if total > line else "UNDER"
        outcome = "WIN" if selection_code == actual else "LOSS"
        return SettlementResult(outcome=outcome, profit_units=0.0)

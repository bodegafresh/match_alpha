from dataclasses import dataclass


@dataclass(frozen=True)
class EvResult:
    market_probability: float
    edge: float
    ev: float
    kelly_fraction: float
    stake_fraction: float


def fractional_kelly(probability: float, decimal_odds: float, fraction: float = 0.25) -> float:
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    if decimal_odds <= 1:
        raise ValueError("decimal_odds must be > 1")
    b = decimal_odds - 1
    raw = (b * probability - (1 - probability)) / b
    return max(0.0, raw * fraction)


def calculate_ev(calibrated_probability: float | None, decimal_odds: float) -> EvResult:
    if calibrated_probability is None:
        raise ValueError("calibrated_probability is required for EV")
    market_probability = 1 / decimal_odds
    edge = calibrated_probability - market_probability
    ev = calibrated_probability * decimal_odds - 1
    kelly = fractional_kelly(calibrated_probability, decimal_odds)
    return EvResult(
        market_probability=market_probability,
        edge=edge,
        ev=ev,
        kelly_fraction=kelly,
        stake_fraction=min(kelly, 0.02),
    )


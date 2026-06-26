"""
Match context features: stage_pressure, rest_days.
These are derived purely from match metadata — no historical match data needed.
"""

from __future__ import annotations

from datetime import date

_STAGE_PRESSURE: dict[str, float] = {
    "FINAL": 1.0,
    "THIRD_PLACE": 0.8,
    "SEMI_FINAL": 0.8,
    "QUARTER_FINAL": 0.6,
    "ROUND_OF_16": 0.5,
    "ROUND_OF_32": 0.4,
    "ROUND_OF_64": 0.3,
    "GROUP_STAGE": 0.2,
    "LEAGUE_PHASE": 0.1,
    "FRIENDLY": 0.0,
}

_MAX_REST_DAYS = 30


def stage_pressure(stage_type: str | None) -> float:
    return _STAGE_PRESSURE.get(stage_type or "", 0.2)


def rest_days(kickoff_date: date, last_match_date: date | None, default: int = 21) -> int:
    if last_match_date is None:
        return default
    return min((kickoff_date - last_match_date).days, _MAX_REST_DAYS)


def feature_completeness(features: dict[str, object]) -> float:
    """
    Fraction of critical features with non-None/non-zero data.
    Weights reflect relative impact on model quality.
    """
    checks = [
        ("elo_global", features.get("elo_global") is not None),
        ("elo_diff", features.get("elo_diff") is not None),
        ("attack_strength", features.get("attack_strength") not in (None, 1.0)),
        ("defense_strength", features.get("defense_strength") not in (None, 1.0)),
        ("form_points", features.get("form_sample_size", 0) >= 3),
        ("rest_days_known", features.get("last_match_date") is not None),
    ]
    n = len(checks)
    filled = sum(1 for _, ok in checks if ok)
    return round(filled / n, 4)

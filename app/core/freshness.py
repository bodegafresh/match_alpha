from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class FreshnessPolicy:
    max_age_minutes: int
    severity_if_stale: str  # INFO | WARN | ERROR | CRITICAL


POLICIES: dict[str, FreshnessPolicy] = {
    "fixtures_upcoming": FreshnessPolicy(max_age_minutes=60, severity_if_stale="WARN"),
    "fixtures_today": FreshnessPolicy(max_age_minutes=15, severity_if_stale="ERROR"),
    "results_yesterday": FreshnessPolicy(max_age_minutes=360, severity_if_stale="WARN"),
    "live_scores": FreshnessPolicy(max_age_minutes=5, severity_if_stale="CRITICAL"),
    "standings": FreshnessPolicy(max_age_minutes=1440, severity_if_stale="WARN"),
    "odds_upcoming": FreshnessPolicy(max_age_minutes=60, severity_if_stale="WARN"),
    "weather": FreshnessPolicy(max_age_minutes=120, severity_if_stale="INFO"),
    "model_predictions": FreshnessPolicy(max_age_minutes=1440, severity_if_stale="WARN"),
    "calibration": FreshnessPolicy(max_age_minutes=1440, severity_if_stale="INFO"),
}


def check_freshness(data_type: str, last_updated_at: datetime | None) -> dict[str, Any]:
    """Returns freshness status for a given data type and last update timestamp."""
    policy = POLICIES.get(data_type)
    if not policy:
        return {"fresh": None, "age_minutes": None, "policy": data_type, "error": "unknown_data_type"}
    if not last_updated_at:
        return {
            "fresh": False,
            "age_minutes": None,
            "max_age_minutes": policy.max_age_minutes,
            "severity_if_stale": policy.severity_if_stale,
            "last_updated_at": None,
        }

    now = datetime.now(UTC)
    if last_updated_at.tzinfo is None:
        last_updated_at = last_updated_at.replace(tzinfo=UTC)

    age_minutes = (now - last_updated_at).total_seconds() / 60
    is_fresh = age_minutes <= policy.max_age_minutes

    return {
        "fresh": is_fresh,
        "age_minutes": round(age_minutes, 1),
        "max_age_minutes": policy.max_age_minutes,
        "severity_if_stale": policy.severity_if_stale if not is_fresh else None,
        "last_updated_at": last_updated_at.isoformat(),
    }

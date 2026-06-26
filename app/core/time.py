from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return ensure_aware_utc(value or utc_now()).isoformat().replace("+00:00", "Z")


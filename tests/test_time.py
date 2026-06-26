from datetime import UTC, datetime

import pytest

from app.core.time import ensure_aware_utc


def test_utc_parser_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        ensure_aware_utc(datetime(2026, 6, 25, 12, 0, 0))


def test_utc_parser_accepts_aware_datetime() -> None:
    value = ensure_aware_utc(datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC))
    assert value.tzinfo == UTC


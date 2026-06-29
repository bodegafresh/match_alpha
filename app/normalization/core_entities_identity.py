from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import iso_utc
from app.normalization.player_identity import normalize_identity_name


def _group_duplicates(keys: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], int] = {}
    for key in keys:
        grouped[key] = grouped.get(key, 0) + 1

    duplicates: list[dict[str, Any]] = []
    for key, count in grouped.items():
        if count <= 1:
            continue
        duplicates.append({"key": list(key), "count": count})
    duplicates.sort(key=lambda item: item["count"], reverse=True)
    return duplicates


async def _load_team_keys(conn: AsyncConnection) -> list[tuple[str, ...]]:
    rows = await conn.execute(
        text(
            """
            SELECT normalized_name, team_type::text, COALESCE(country_code, '') AS country_code
            FROM teams
            WHERE COALESCE(normalized_name, '') <> ''
            """
        )
    )
    out: list[tuple[str, ...]] = []
    for row in rows:
        m = row._mapping
        out.append((str(m["normalized_name"]), str(m["team_type"]), str(m["country_code"])))
    return out


async def _load_referee_keys(conn: AsyncConnection) -> list[tuple[str, ...]]:
    rows = await conn.execute(
        text(
            """
            SELECT normalized_name, COALESCE(nationality_country_code, '') AS nationality_country_code
            FROM referees
            WHERE COALESCE(normalized_name, '') <> ''
            """
        )
    )
    out: list[tuple[str, ...]] = []
    for row in rows:
        m = row._mapping
        out.append((str(m["normalized_name"]), str(m["nationality_country_code"])))
    return out


async def _load_venue_keys(conn: AsyncConnection) -> list[tuple[str, ...]]:
    rows = await conn.execute(
        text(
            """
            SELECT display_name, COALESCE(city, '') AS city, COALESCE(country_code, '') AS country_code
            FROM venues
            WHERE COALESCE(display_name, '') <> ''
            """
        )
    )
    out: list[tuple[str, ...]] = []
    for row in rows:
        m = row._mapping
        out.append(
            (
                normalize_identity_name(str(m["display_name"])),
                normalize_identity_name(str(m["city"])),
                str(m["country_code"]),
            )
        )
    return out


async def validate_core_entities_identity_consistency(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate duplicate identity candidates for teams, referees and venues.

    This validation is read-only and intended as a guardrail after weekly sync.
    """
    max_team_duplicates = int(payload.get("max_team_duplicates", 150) or 150)
    max_referee_duplicates = int(payload.get("max_referee_duplicates", 80) or 80)
    max_venue_duplicates = int(payload.get("max_venue_duplicates", 80) or 80)

    team_keys = await _load_team_keys(conn)
    referee_keys = await _load_referee_keys(conn)
    venue_keys = await _load_venue_keys(conn)

    team_duplicates = _group_duplicates(team_keys)
    referee_duplicates = _group_duplicates(referee_keys)
    venue_duplicates = _group_duplicates(venue_keys)

    warnings: list[str] = []
    if len(team_duplicates) > max_team_duplicates:
        warnings.append("TEAM_DUPLICATES_ABOVE_THRESHOLD")
    if len(referee_duplicates) > max_referee_duplicates:
        warnings.append("REFEREE_DUPLICATES_ABOVE_THRESHOLD")
    if len(venue_duplicates) > max_venue_duplicates:
        warnings.append("VENUE_DUPLICATES_ABOVE_THRESHOLD")

    return {
        "status": "WARN" if warnings else "OK",
        "job_name": "validate_core_entities_identity",
        "records_processed": len(team_keys) + len(referee_keys) + len(venue_keys),
        "warnings": warnings,
        "summary": {
            "teams_scanned": len(team_keys),
            "team_duplicate_groups": len(team_duplicates),
            "referees_scanned": len(referee_keys),
            "referee_duplicate_groups": len(referee_duplicates),
            "venues_scanned": len(venue_keys),
            "venue_duplicate_groups": len(venue_duplicates),
        },
        "thresholds": {
            "max_team_duplicates": max_team_duplicates,
            "max_referee_duplicates": max_referee_duplicates,
            "max_venue_duplicates": max_venue_duplicates,
        },
        "duplicates_preview": {
            "teams": team_duplicates[:50],
            "referees": referee_duplicates[:50],
            "venues": venue_duplicates[:50],
        },
        "generated_at": iso_utc(),
    }

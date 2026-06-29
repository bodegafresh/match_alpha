from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import iso_utc
from app.normalization.player_identity import normalize_identity_name


@dataclass
class _TeamRow:
    team_id: str
    slug: str | None
    display_name: str
    normalized_name: str
    team_type: str
    country_code: str | None
    metadata: dict[str, Any]
    created_at: Any
    alias_count: int
    ref_count: int


@dataclass
class _RefereeRow:
    referee_id: str
    slug: str | None
    display_name: str
    normalized_name: str
    nationality_country_code: str | None
    metadata: dict[str, Any]
    created_at: Any
    ref_count: int


@dataclass
class _VenueRow:
    venue_id: str
    slug: str | None
    display_name: str
    city: str | None
    country_code: str | None
    timezone_name: str | None
    latitude: Any
    longitude: Any
    metadata: dict[str, Any]
    created_at: Any
    ref_count: int


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _prefer_text(current: str | None, candidate: str | None) -> str | None:
    current = (current or "").strip()
    candidate = (candidate or "").strip()
    if not current:
        return candidate or None
    if not candidate:
        return current
    return candidate if len(candidate) > len(current) else current


def _team_score(row: _TeamRow) -> int:
    score = 0
    score += min(int(row.alias_count), 10)
    score += min(int(row.ref_count) * 2, 20)
    if row.country_code:
        score += 5
    if row.slug:
        score += 2
    return score


def _referee_score(row: _RefereeRow) -> int:
    score = min(int(row.ref_count) * 2, 20)
    if row.nationality_country_code:
        score += 5
    if row.slug:
        score += 2
    return score


def _venue_score(row: _VenueRow) -> int:
    score = min(int(row.ref_count) * 2, 20)
    if row.city:
        score += 8
    if row.country_code:
        score += 5
    if row.timezone_name:
        score += 2
    if row.latitude is not None and row.longitude is not None:
        score += 3
    return score


async def _load_teams(conn: AsyncConnection) -> list[_TeamRow]:
    rows = await conn.execute(
        text(
            """
            SELECT
              t.team_id::text,
              t.slug,
              t.display_name,
              t.normalized_name,
              t.team_type::text,
              t.country_code,
              t.metadata,
              t.created_at,
              COALESCE(a.alias_count, 0)::int AS alias_count,
              COALESCE(r.ref_count, 0)::int AS ref_count
            FROM teams t
            LEFT JOIN (
              SELECT team_id, COUNT(*)::int AS alias_count
              FROM team_aliases
              GROUP BY team_id
            ) a ON a.team_id = t.team_id
            LEFT JOIN (
              SELECT entity_id AS team_id, COUNT(*)::int AS ref_count
              FROM entity_external_refs
              WHERE entity_type = 'TEAM'
              GROUP BY entity_id
            ) r ON r.team_id = t.team_id
            """
        )
    )
    out: list[_TeamRow] = []
    for row in rows:
        m = row._mapping
        out.append(
            _TeamRow(
                team_id=str(m["team_id"]),
                slug=m.get("slug"),
                display_name=str(m["display_name"]),
                normalized_name=str(m["normalized_name"]),
                team_type=str(m["team_type"]),
                country_code=m.get("country_code"),
                metadata=m.get("metadata") or {},
                created_at=m.get("created_at"),
                alias_count=int(m.get("alias_count") or 0),
                ref_count=int(m.get("ref_count") or 0),
            )
        )
    return out


async def _load_referees(conn: AsyncConnection) -> list[_RefereeRow]:
    rows = await conn.execute(
        text(
            """
            SELECT
              r.referee_id::text,
              r.slug,
              r.display_name,
              r.normalized_name,
              r.nationality_country_code,
              r.metadata,
              r.created_at,
              COALESCE(er.ref_count, 0)::int AS ref_count
            FROM referees r
            LEFT JOIN (
              SELECT entity_id AS referee_id, COUNT(*)::int AS ref_count
              FROM entity_external_refs
              WHERE entity_type = 'REFEREE'
              GROUP BY entity_id
            ) er ON er.referee_id = r.referee_id
            """
        )
    )
    out: list[_RefereeRow] = []
    for row in rows:
        m = row._mapping
        out.append(
            _RefereeRow(
                referee_id=str(m["referee_id"]),
                slug=m.get("slug"),
                display_name=str(m["display_name"]),
                normalized_name=str(m["normalized_name"]),
                nationality_country_code=m.get("nationality_country_code"),
                metadata=m.get("metadata") or {},
                created_at=m.get("created_at"),
                ref_count=int(m.get("ref_count") or 0),
            )
        )
    return out


async def _load_venues(conn: AsyncConnection) -> list[_VenueRow]:
    rows = await conn.execute(
        text(
            """
            SELECT
              v.venue_id::text,
              v.slug,
              v.display_name,
              v.city,
              v.country_code,
              v.timezone_name,
              v.latitude,
              v.longitude,
              v.metadata,
              v.created_at,
              COALESCE(er.ref_count, 0)::int AS ref_count
            FROM venues v
            LEFT JOIN (
              SELECT entity_id AS venue_id, COUNT(*)::int AS ref_count
              FROM entity_external_refs
              WHERE entity_type = 'VENUE'
              GROUP BY entity_id
            ) er ON er.venue_id = v.venue_id
            """
        )
    )
    out: list[_VenueRow] = []
    for row in rows:
        m = row._mapping
        out.append(
            _VenueRow(
                venue_id=str(m["venue_id"]),
                slug=m.get("slug"),
                display_name=str(m["display_name"]),
                city=m.get("city"),
                country_code=m.get("country_code"),
                timezone_name=m.get("timezone_name"),
                latitude=m.get("latitude"),
                longitude=m.get("longitude"),
                metadata=m.get("metadata") or {},
                created_at=m.get("created_at"),
                ref_count=int(m.get("ref_count") or 0),
            )
        )
    return out


def _build_team_merge_plan(rows: list[_TeamRow], country_code: str | None = None) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str, str], list[_TeamRow]] = {}
    for row in rows:
        if not row.normalized_name:
            continue
        if country_code and (row.country_code or "") != country_code:
            continue
        key = (row.team_type, row.country_code or "", row.normalized_name)
        grouped.setdefault(key, []).append(row)

    plan: list[dict[str, str]] = []
    for key_rows in grouped.values():
        if len(key_rows) <= 1:
            continue
        master = sorted(key_rows, key=lambda x: (_team_score(x), x.created_at), reverse=True)[0]
        for row in key_rows:
            if row.team_id == master.team_id:
                continue
            plan.append(
                {
                    "source_team_id": row.team_id,
                    "target_team_id": master.team_id,
                    "reason": "EXACT_NORMALIZED_TEAM",
                }
            )
    return plan


def _build_referee_merge_plan(rows: list[_RefereeRow], country_code: str | None = None) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], list[_RefereeRow]] = {}
    for row in rows:
        if not row.normalized_name:
            continue
        if country_code and (row.nationality_country_code or "") != country_code:
            continue
        key = (row.nationality_country_code or "", row.normalized_name)
        grouped.setdefault(key, []).append(row)

    plan: list[dict[str, str]] = []
    for key_rows in grouped.values():
        if len(key_rows) <= 1:
            continue
        master = sorted(key_rows, key=lambda x: (_referee_score(x), x.created_at), reverse=True)[0]
        for row in key_rows:
            if row.referee_id == master.referee_id:
                continue
            plan.append(
                {
                    "source_referee_id": row.referee_id,
                    "target_referee_id": master.referee_id,
                    "reason": "EXACT_NORMALIZED_REFEREE",
                }
            )
    return plan


def _build_venue_merge_plan(rows: list[_VenueRow], country_code: str | None = None) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str, str], list[_VenueRow]] = {}
    for row in rows:
        if not row.display_name:
            continue
        if country_code and (row.country_code or "") != country_code:
            continue
        key = (
            row.country_code or "",
            normalize_identity_name(row.city or ""),
            normalize_identity_name(row.display_name),
        )
        grouped.setdefault(key, []).append(row)

    plan: list[dict[str, str]] = []
    for key_rows in grouped.values():
        if len(key_rows) <= 1:
            continue
        master = sorted(key_rows, key=lambda x: (_venue_score(x), x.created_at), reverse=True)[0]
        for row in key_rows:
            if row.venue_id == master.venue_id:
                continue
            plan.append(
                {
                    "source_venue_id": row.venue_id,
                    "target_venue_id": master.venue_id,
                    "reason": "EXACT_NORMALIZED_VENUE_CITY_COUNTRY",
                }
            )
    return plan


async def _merge_team_one(conn: AsyncConnection, source_team_id: str, target_team_id: str, reason: str) -> dict[str, Any]:
    if source_team_id == target_team_id:
        return {"merged": False, "reason": "SOURCE_EQUALS_TARGET"}

    source_row = (
        await conn.execute(
            text(
                """
                SELECT team_id::text, slug, display_name, normalized_name, team_type::text, country_code, metadata
                FROM teams
                WHERE team_id = cast(:team_id as uuid)
                LIMIT 1
                """
            ),
            {"team_id": source_team_id},
        )
    ).mappings().first()
    if not source_row:
        return {"merged": False, "reason": "SOURCE_NOT_FOUND"}

    target_row = (
        await conn.execute(
            text(
                """
                SELECT team_id::text, slug, display_name, normalized_name, team_type::text, country_code, metadata
                FROM teams
                WHERE team_id = cast(:team_id as uuid)
                LIMIT 1
                """
            ),
            {"team_id": target_team_id},
        )
    ).mappings().first()
    if not target_row:
        return {"merged": False, "reason": "TARGET_NOT_FOUND"}

    await conn.execute(
        text(
            """
            INSERT INTO team_aliases
              (team_id, alias, normalized_alias, language_code, source, confidence, created_at, updated_at)
            SELECT
              cast(:target_team_id as uuid),
              alias,
              normalized_alias,
              language_code,
              source,
              confidence,
              created_at,
              now()
            FROM team_aliases
            WHERE team_id = cast(:source_team_id as uuid)
            ON CONFLICT (normalized_alias, source) DO UPDATE SET
              team_id = excluded.team_id,
              alias = excluded.alias,
              confidence = GREATEST(team_aliases.confidence, excluded.confidence),
              updated_at = now()
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text("DELETE FROM team_aliases WHERE team_id = cast(:source_team_id as uuid)"),
        {"source_team_id": source_team_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO entity_external_refs
              (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name,
               source_url, confidence, is_primary, payload, created_at, updated_at)
            SELECT
              entity_type,
              cast(:target_team_id as uuid),
              source,
              source_entity_type,
              source_entity_id,
              source_entity_name,
              source_url,
              confidence,
              is_primary,
              payload,
              created_at,
              now()
            FROM entity_external_refs
            WHERE entity_type = 'TEAM'
              AND entity_id = cast(:source_team_id as uuid)
            ON CONFLICT (entity_type, source, source_entity_id) DO UPDATE SET
              entity_id = excluded.entity_id,
              source_entity_name = COALESCE(entity_external_refs.source_entity_name, excluded.source_entity_name),
              source_url = COALESCE(entity_external_refs.source_url, excluded.source_url),
              confidence = GREATEST(entity_external_refs.confidence, excluded.confidence),
              is_primary = entity_external_refs.is_primary OR excluded.is_primary,
              payload = entity_external_refs.payload || excluded.payload,
              updated_at = now()
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM entity_external_refs
            WHERE entity_type = 'TEAM'
              AND entity_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO competition_team_entries
              (competition_season_id, team_id, entry_status, seed_rating, metadata, created_at, updated_at)
            SELECT
              competition_season_id,
              cast(:target_team_id as uuid),
              entry_status,
              seed_rating,
              metadata,
              created_at,
              now()
            FROM competition_team_entries
            WHERE team_id = cast(:source_team_id as uuid)
            ON CONFLICT (competition_season_id, team_id) DO UPDATE SET
              entry_status = CASE
                WHEN competition_team_entries.entry_status = 'ACTIVE'::group_membership_status THEN competition_team_entries.entry_status
                WHEN excluded.entry_status = 'ACTIVE'::group_membership_status THEN excluded.entry_status
                ELSE competition_team_entries.entry_status
              END,
              seed_rating = COALESCE(competition_team_entries.seed_rating, excluded.seed_rating),
              metadata = competition_team_entries.metadata || excluded.metadata,
              updated_at = now()
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text("DELETE FROM competition_team_entries WHERE team_id = cast(:source_team_id as uuid)"),
        {"source_team_id": source_team_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO competition_rosters
              (competition_season_id, team_id, player_id, shirt_number, position, roster_status, metadata, created_at, updated_at)
            SELECT
              competition_season_id,
              cast(:target_team_id as uuid),
              player_id,
              shirt_number,
              position,
              roster_status,
              metadata,
              created_at,
              now()
            FROM competition_rosters
            WHERE team_id = cast(:source_team_id as uuid)
            ON CONFLICT (competition_season_id, team_id, player_id) DO UPDATE SET
              shirt_number = COALESCE(competition_rosters.shirt_number, excluded.shirt_number),
              position = COALESCE(competition_rosters.position, excluded.position),
              roster_status = CASE
                WHEN competition_rosters.roster_status IN ('ACTIVE', 'CALLED_UP') THEN competition_rosters.roster_status
                WHEN excluded.roster_status IN ('ACTIVE', 'CALLED_UP') THEN excluded.roster_status
                ELSE competition_rosters.roster_status
              END,
              metadata = competition_rosters.metadata || excluded.metadata,
              updated_at = now()
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text("DELETE FROM competition_rosters WHERE team_id = cast(:source_team_id as uuid)"),
        {"source_team_id": source_team_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO team_memberships
              (player_id, team_id, membership_type, valid_from_at, valid_to_at, source, confidence, metadata, created_at, updated_at)
            SELECT
              player_id,
              cast(:target_team_id as uuid),
              membership_type,
              valid_from_at,
              valid_to_at,
              source,
              confidence,
              metadata,
              created_at,
              now()
            FROM team_memberships
            WHERE team_id = cast(:source_team_id as uuid)
            ON CONFLICT (player_id, team_id, membership_type, source) DO UPDATE SET
              valid_from_at = COALESCE(LEAST(team_memberships.valid_from_at, excluded.valid_from_at), team_memberships.valid_from_at, excluded.valid_from_at),
              valid_to_at = CASE
                WHEN team_memberships.valid_to_at IS NULL OR excluded.valid_to_at IS NULL THEN NULL
                ELSE GREATEST(team_memberships.valid_to_at, excluded.valid_to_at)
              END,
              confidence = GREATEST(team_memberships.confidence, excluded.confidence),
              metadata = team_memberships.metadata || excluded.metadata,
              updated_at = now()
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text("DELETE FROM team_memberships WHERE team_id = cast(:source_team_id as uuid)"),
        {"source_team_id": source_team_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO match_lineups
              (match_id, team_id, player_id, lineup_role, position, shirt_number, is_captain, source, metadata, created_at, updated_at)
            SELECT
              match_id,
              cast(:target_team_id as uuid),
              player_id,
              lineup_role,
              position,
              shirt_number,
              is_captain,
              source,
              metadata,
              created_at,
              now()
            FROM match_lineups
            WHERE team_id = cast(:source_team_id as uuid)
            ON CONFLICT (match_id, team_id, player_id, source) DO UPDATE SET
              lineup_role = COALESCE(match_lineups.lineup_role, excluded.lineup_role),
              position = COALESCE(match_lineups.position, excluded.position),
              shirt_number = COALESCE(match_lineups.shirt_number, excluded.shirt_number),
              is_captain = match_lineups.is_captain OR excluded.is_captain,
              metadata = match_lineups.metadata || excluded.metadata,
              updated_at = now()
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text("DELETE FROM match_lineups WHERE team_id = cast(:source_team_id as uuid)"),
        {"source_team_id": source_team_id},
    )

    await conn.execute(
        text(
            """
            UPDATE tournament_slots
            SET resolved_team_id = cast(:target_team_id as uuid)
            WHERE resolved_team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE matches
            SET winner_team_id = cast(:target_team_id as uuid)
            WHERE winner_team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE match_participants
            SET team_id = cast(:target_team_id as uuid)
            WHERE team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE match_events
            SET team_id = cast(:target_team_id as uuid)
            WHERE team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE player_match_stats
            SET team_id = cast(:target_team_id as uuid)
            WHERE team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE standings
            SET team_id = cast(:target_team_id as uuid)
            WHERE team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE feature_snapshots
            SET team_id = cast(:target_team_id as uuid)
            WHERE team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE rating_snapshots
            SET team_id = cast(:target_team_id as uuid)
            WHERE team_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )
    await conn.execute(
        text(
            """
            UPDATE entity_resolution_queue
            SET resolved_entity_id = cast(:target_team_id as uuid)
            WHERE entity_type = 'TEAM'
              AND resolved_entity_id = cast(:source_team_id as uuid)
            """
        ),
        {"source_team_id": source_team_id, "target_team_id": target_team_id},
    )

    target_metadata = dict(target_row.get("metadata") or {})
    source_metadata = dict(source_row.get("metadata") or {})
    merged_from = list(target_metadata.get("merged_team_ids") or [])
    if source_team_id not in merged_from:
        merged_from.append(source_team_id)
    source_slugs = list(target_metadata.get("source_slugs") or [])
    if source_row.get("slug") and source_row["slug"] not in source_slugs:
        source_slugs.append(source_row["slug"])

    merged_metadata = {
        **target_metadata,
        **source_metadata,
        "source_slugs": source_slugs,
        "merged_team_ids": merged_from,
        "last_merge_reason": reason,
        "last_merged_at": datetime.now(timezone.utc).isoformat(),
    }

    display_name = _prefer_text(target_row.get("display_name"), source_row.get("display_name"))
    normalized_name = normalize_identity_name(display_name or target_row.get("display_name") or "")

    await conn.execute(
        text(
            """
            UPDATE teams
            SET
              display_name = COALESCE(:display_name, display_name),
              normalized_name = COALESCE(:normalized_name, normalized_name),
              country_code = COALESCE(country_code, :country_code),
              metadata = cast(:metadata as jsonb),
              updated_at = now()
            WHERE team_id = cast(:target_team_id as uuid)
            """
        ),
        {
            "target_team_id": target_team_id,
            "display_name": display_name,
            "normalized_name": normalized_name,
            "country_code": source_row.get("country_code"),
            "metadata": _json(merged_metadata),
        },
    )

    await conn.execute(
        text("DELETE FROM teams WHERE team_id = cast(:source_team_id as uuid)"),
        {"source_team_id": source_team_id},
    )

    return {
        "merged": True,
        "source_team_id": source_team_id,
        "target_team_id": target_team_id,
        "reason": reason,
    }


async def _merge_referee_one(conn: AsyncConnection, source_referee_id: str, target_referee_id: str, reason: str) -> dict[str, Any]:
    if source_referee_id == target_referee_id:
        return {"merged": False, "reason": "SOURCE_EQUALS_TARGET"}

    source_row = (
        await conn.execute(
            text(
                """
                SELECT referee_id::text, slug, display_name, normalized_name, nationality_country_code, metadata
                FROM referees
                WHERE referee_id = cast(:referee_id as uuid)
                LIMIT 1
                """
            ),
            {"referee_id": source_referee_id},
        )
    ).mappings().first()
    if not source_row:
        return {"merged": False, "reason": "SOURCE_NOT_FOUND"}

    target_row = (
        await conn.execute(
            text(
                """
                SELECT referee_id::text, slug, display_name, normalized_name, nationality_country_code, metadata
                FROM referees
                WHERE referee_id = cast(:referee_id as uuid)
                LIMIT 1
                """
            ),
            {"referee_id": target_referee_id},
        )
    ).mappings().first()
    if not target_row:
        return {"merged": False, "reason": "TARGET_NOT_FOUND"}

    await conn.execute(
        text(
            """
            INSERT INTO entity_external_refs
              (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name,
               source_url, confidence, is_primary, payload, created_at, updated_at)
            SELECT
              entity_type,
              cast(:target_referee_id as uuid),
              source,
              source_entity_type,
              source_entity_id,
              source_entity_name,
              source_url,
              confidence,
              is_primary,
              payload,
              created_at,
              now()
            FROM entity_external_refs
            WHERE entity_type = 'REFEREE'
              AND entity_id = cast(:source_referee_id as uuid)
            ON CONFLICT (entity_type, source, source_entity_id) DO UPDATE SET
              entity_id = excluded.entity_id,
              source_entity_name = COALESCE(entity_external_refs.source_entity_name, excluded.source_entity_name),
              source_url = COALESCE(entity_external_refs.source_url, excluded.source_url),
              confidence = GREATEST(entity_external_refs.confidence, excluded.confidence),
              is_primary = entity_external_refs.is_primary OR excluded.is_primary,
              payload = entity_external_refs.payload || excluded.payload,
              updated_at = now()
            """
        ),
        {"source_referee_id": source_referee_id, "target_referee_id": target_referee_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM entity_external_refs
            WHERE entity_type = 'REFEREE'
              AND entity_id = cast(:source_referee_id as uuid)
            """
        ),
        {"source_referee_id": source_referee_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO match_officials
              (match_id, referee_id, role, metadata, created_at)
            SELECT
              match_id,
              cast(:target_referee_id as uuid),
              role,
              metadata,
              created_at
            FROM match_officials
            WHERE referee_id = cast(:source_referee_id as uuid)
            ON CONFLICT (match_id, referee_id, role) DO UPDATE SET
              metadata = match_officials.metadata || excluded.metadata
            """
        ),
        {"source_referee_id": source_referee_id, "target_referee_id": target_referee_id},
    )
    await conn.execute(
        text("DELETE FROM match_officials WHERE referee_id = cast(:source_referee_id as uuid)"),
        {"source_referee_id": source_referee_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO entity_media_assets
              (entity_type, entity_id, media_type, source, url, is_primary, width, height, mime_type, payload, created_at, updated_at)
            SELECT
              entity_type,
              cast(:target_referee_id as uuid),
              media_type,
              source,
              url,
              is_primary,
              width,
              height,
              mime_type,
              payload,
              created_at,
              now()
            FROM entity_media_assets
            WHERE entity_type = 'REFEREE'
              AND entity_id = cast(:source_referee_id as uuid)
            ON CONFLICT (entity_type, entity_id, media_type, source) DO UPDATE SET
              url = COALESCE(entity_media_assets.url, excluded.url),
              is_primary = entity_media_assets.is_primary OR excluded.is_primary,
              width = COALESCE(entity_media_assets.width, excluded.width),
              height = COALESCE(entity_media_assets.height, excluded.height),
              mime_type = COALESCE(entity_media_assets.mime_type, excluded.mime_type),
              payload = entity_media_assets.payload || excluded.payload,
              updated_at = now()
            """
        ),
        {"source_referee_id": source_referee_id, "target_referee_id": target_referee_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM entity_media_assets
            WHERE entity_type = 'REFEREE'
              AND entity_id = cast(:source_referee_id as uuid)
            """
        ),
        {"source_referee_id": source_referee_id},
    )

    await conn.execute(
        text(
            """
            UPDATE entity_resolution_queue
            SET resolved_entity_id = cast(:target_referee_id as uuid)
            WHERE entity_type = 'REFEREE'
              AND resolved_entity_id = cast(:source_referee_id as uuid)
            """
        ),
        {"source_referee_id": source_referee_id, "target_referee_id": target_referee_id},
    )

    target_metadata = dict(target_row.get("metadata") or {})
    source_metadata = dict(source_row.get("metadata") or {})
    merged_from = list(target_metadata.get("merged_referee_ids") or [])
    if source_referee_id not in merged_from:
        merged_from.append(source_referee_id)

    merged_metadata = {
        **target_metadata,
        **source_metadata,
        "merged_referee_ids": merged_from,
        "last_merge_reason": reason,
        "last_merged_at": datetime.now(timezone.utc).isoformat(),
    }

    display_name = _prefer_text(target_row.get("display_name"), source_row.get("display_name"))
    normalized_name = normalize_identity_name(display_name or target_row.get("display_name") or "")

    await conn.execute(
        text(
            """
            UPDATE referees
            SET
              display_name = COALESCE(:display_name, display_name),
              normalized_name = COALESCE(:normalized_name, normalized_name),
              nationality_country_code = COALESCE(nationality_country_code, :nationality_country_code),
              metadata = cast(:metadata as jsonb),
              updated_at = now()
            WHERE referee_id = cast(:target_referee_id as uuid)
            """
        ),
        {
            "target_referee_id": target_referee_id,
            "display_name": display_name,
            "normalized_name": normalized_name,
            "nationality_country_code": source_row.get("nationality_country_code"),
            "metadata": _json(merged_metadata),
        },
    )

    await conn.execute(
        text("DELETE FROM referees WHERE referee_id = cast(:source_referee_id as uuid)"),
        {"source_referee_id": source_referee_id},
    )

    return {
        "merged": True,
        "source_referee_id": source_referee_id,
        "target_referee_id": target_referee_id,
        "reason": reason,
    }


async def _merge_venue_one(conn: AsyncConnection, source_venue_id: str, target_venue_id: str, reason: str) -> dict[str, Any]:
    if source_venue_id == target_venue_id:
        return {"merged": False, "reason": "SOURCE_EQUALS_TARGET"}

    source_row = (
        await conn.execute(
            text(
                """
                SELECT venue_id::text, slug, display_name, city, country_code, timezone_name, latitude, longitude, metadata
                FROM venues
                WHERE venue_id = cast(:venue_id as uuid)
                LIMIT 1
                """
            ),
            {"venue_id": source_venue_id},
        )
    ).mappings().first()
    if not source_row:
        return {"merged": False, "reason": "SOURCE_NOT_FOUND"}

    target_row = (
        await conn.execute(
            text(
                """
                SELECT venue_id::text, slug, display_name, city, country_code, timezone_name, latitude, longitude, metadata
                FROM venues
                WHERE venue_id = cast(:venue_id as uuid)
                LIMIT 1
                """
            ),
            {"venue_id": target_venue_id},
        )
    ).mappings().first()
    if not target_row:
        return {"merged": False, "reason": "TARGET_NOT_FOUND"}

    await conn.execute(
        text(
            """
            INSERT INTO entity_external_refs
              (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name,
               source_url, confidence, is_primary, payload, created_at, updated_at)
            SELECT
              entity_type,
              cast(:target_venue_id as uuid),
              source,
              source_entity_type,
              source_entity_id,
              source_entity_name,
              source_url,
              confidence,
              is_primary,
              payload,
              created_at,
              now()
            FROM entity_external_refs
            WHERE entity_type = 'VENUE'
              AND entity_id = cast(:source_venue_id as uuid)
            ON CONFLICT (entity_type, source, source_entity_id) DO UPDATE SET
              entity_id = excluded.entity_id,
              source_entity_name = COALESCE(entity_external_refs.source_entity_name, excluded.source_entity_name),
              source_url = COALESCE(entity_external_refs.source_url, excluded.source_url),
              confidence = GREATEST(entity_external_refs.confidence, excluded.confidence),
              is_primary = entity_external_refs.is_primary OR excluded.is_primary,
              payload = entity_external_refs.payload || excluded.payload,
              updated_at = now()
            """
        ),
        {"source_venue_id": source_venue_id, "target_venue_id": target_venue_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM entity_external_refs
            WHERE entity_type = 'VENUE'
              AND entity_id = cast(:source_venue_id as uuid)
            """
        ),
        {"source_venue_id": source_venue_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO entity_media_assets
              (entity_type, entity_id, media_type, source, url, is_primary, width, height, mime_type, payload, created_at, updated_at)
            SELECT
              entity_type,
              cast(:target_venue_id as uuid),
              media_type,
              source,
              url,
              is_primary,
              width,
              height,
              mime_type,
              payload,
              created_at,
              now()
            FROM entity_media_assets
            WHERE entity_type = 'VENUE'
              AND entity_id = cast(:source_venue_id as uuid)
            ON CONFLICT (entity_type, entity_id, media_type, source) DO UPDATE SET
              url = COALESCE(entity_media_assets.url, excluded.url),
              is_primary = entity_media_assets.is_primary OR excluded.is_primary,
              width = COALESCE(entity_media_assets.width, excluded.width),
              height = COALESCE(entity_media_assets.height, excluded.height),
              mime_type = COALESCE(entity_media_assets.mime_type, excluded.mime_type),
              payload = entity_media_assets.payload || excluded.payload,
              updated_at = now()
            """
        ),
        {"source_venue_id": source_venue_id, "target_venue_id": target_venue_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM entity_media_assets
            WHERE entity_type = 'VENUE'
              AND entity_id = cast(:source_venue_id as uuid)
            """
        ),
        {"source_venue_id": source_venue_id},
    )

    await conn.execute(
        text(
            """
            UPDATE matches
            SET venue_id = cast(:target_venue_id as uuid)
            WHERE venue_id = cast(:source_venue_id as uuid)
            """
        ),
        {"source_venue_id": source_venue_id, "target_venue_id": target_venue_id},
    )
    await conn.execute(
        text(
            """
            UPDATE entity_resolution_queue
            SET resolved_entity_id = cast(:target_venue_id as uuid)
            WHERE entity_type = 'VENUE'
              AND resolved_entity_id = cast(:source_venue_id as uuid)
            """
        ),
        {"source_venue_id": source_venue_id, "target_venue_id": target_venue_id},
    )

    target_metadata = dict(target_row.get("metadata") or {})
    source_metadata = dict(source_row.get("metadata") or {})
    merged_from = list(target_metadata.get("merged_venue_ids") or [])
    if source_venue_id not in merged_from:
        merged_from.append(source_venue_id)

    merged_metadata = {
        **target_metadata,
        **source_metadata,
        "merged_venue_ids": merged_from,
        "last_merge_reason": reason,
        "last_merged_at": datetime.now(timezone.utc).isoformat(),
    }

    display_name = _prefer_text(target_row.get("display_name"), source_row.get("display_name"))
    city = _prefer_text(target_row.get("city"), source_row.get("city"))

    await conn.execute(
        text(
            """
            UPDATE venues
            SET
              display_name = COALESCE(:display_name, display_name),
              city = COALESCE(:city, city),
              country_code = COALESCE(country_code, :country_code),
              timezone_name = COALESCE(timezone_name, :timezone_name),
              latitude = COALESCE(latitude, :latitude),
              longitude = COALESCE(longitude, :longitude),
              metadata = cast(:metadata as jsonb),
              updated_at = now()
            WHERE venue_id = cast(:target_venue_id as uuid)
            """
        ),
        {
            "target_venue_id": target_venue_id,
            "display_name": display_name,
            "city": city,
            "country_code": source_row.get("country_code"),
            "timezone_name": source_row.get("timezone_name"),
            "latitude": source_row.get("latitude"),
            "longitude": source_row.get("longitude"),
            "metadata": _json(merged_metadata),
        },
    )

    await conn.execute(
        text("DELETE FROM venues WHERE venue_id = cast(:source_venue_id as uuid)"),
        {"source_venue_id": source_venue_id},
    )

    return {
        "merged": True,
        "source_venue_id": source_venue_id,
        "target_venue_id": target_venue_id,
        "reason": reason,
    }


async def reconcile_teams_identity(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    dry_run = bool(payload.get("dry_run", False))
    limit_merges = int(payload.get("limit_merges", 500) or 500)
    max_runtime_seconds = int(payload.get("max_runtime_seconds", 60) or 60)
    country_code = payload.get("country_code")
    country_code = str(country_code).upper() if country_code else None
    started_at = perf_counter()

    rows = await _load_teams(conn)
    plan = _build_team_merge_plan(rows, country_code=country_code)
    if limit_merges > 0:
        plan = plan[:limit_merges]

    if dry_run:
        return {
            "status": "OK",
            "job_name": "reconcile_teams_identity",
            "records_processed": 0,
            "dry_run": True,
            "summary": {
                "teams_scanned": len(rows),
                "merge_candidates": len(plan),
                "country_code": country_code,
            },
            "merge_plan": plan[:300],
            "generated_at": iso_utc(),
        }

    await conn.execute(text("SELECT pg_advisory_xact_lock(74500124)"))

    merged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in plan:
        if (perf_counter() - started_at) >= max_runtime_seconds:
            skipped.append({"merged": False, "reason": "TIME_BUDGET_EXCEEDED"})
            break
        result = await _merge_team_one(
            conn,
            source_team_id=item["source_team_id"],
            target_team_id=item["target_team_id"],
            reason=item["reason"],
        )
        (merged if result.get("merged") else skipped).append(result)

    return {
        "status": "OK",
        "job_name": "reconcile_teams_identity",
        "records_processed": len(merged),
        "dry_run": False,
        "summary": {
            "teams_scanned": len(rows),
            "merge_candidates": len(plan),
            "merged": len(merged),
            "skipped": len(skipped),
            "country_code": country_code,
            "elapsed_seconds": int(perf_counter() - started_at),
            "max_runtime_seconds": max_runtime_seconds,
        },
        "merged_pairs": merged[:500],
        "skipped_pairs": skipped[:200],
        "generated_at": iso_utc(),
    }


async def reconcile_referees_identity(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    dry_run = bool(payload.get("dry_run", False))
    limit_merges = int(payload.get("limit_merges", 300) or 300)
    max_runtime_seconds = int(payload.get("max_runtime_seconds", 45) or 45)
    country_code = payload.get("country_code")
    country_code = str(country_code).upper() if country_code else None
    started_at = perf_counter()

    rows = await _load_referees(conn)
    plan = _build_referee_merge_plan(rows, country_code=country_code)
    if limit_merges > 0:
        plan = plan[:limit_merges]

    if dry_run:
        return {
            "status": "OK",
            "job_name": "reconcile_referees_identity",
            "records_processed": 0,
            "dry_run": True,
            "summary": {
                "referees_scanned": len(rows),
                "merge_candidates": len(plan),
                "country_code": country_code,
            },
            "merge_plan": plan[:300],
            "generated_at": iso_utc(),
        }

    await conn.execute(text("SELECT pg_advisory_xact_lock(74500125)"))

    merged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in plan:
        if (perf_counter() - started_at) >= max_runtime_seconds:
            skipped.append({"merged": False, "reason": "TIME_BUDGET_EXCEEDED"})
            break
        result = await _merge_referee_one(
            conn,
            source_referee_id=item["source_referee_id"],
            target_referee_id=item["target_referee_id"],
            reason=item["reason"],
        )
        (merged if result.get("merged") else skipped).append(result)

    return {
        "status": "OK",
        "job_name": "reconcile_referees_identity",
        "records_processed": len(merged),
        "dry_run": False,
        "summary": {
            "referees_scanned": len(rows),
            "merge_candidates": len(plan),
            "merged": len(merged),
            "skipped": len(skipped),
            "country_code": country_code,
            "elapsed_seconds": int(perf_counter() - started_at),
            "max_runtime_seconds": max_runtime_seconds,
        },
        "merged_pairs": merged[:500],
        "skipped_pairs": skipped[:200],
        "generated_at": iso_utc(),
    }


async def reconcile_venues_identity(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    dry_run = bool(payload.get("dry_run", False))
    limit_merges = int(payload.get("limit_merges", 300) or 300)
    max_runtime_seconds = int(payload.get("max_runtime_seconds", 45) or 45)
    country_code = payload.get("country_code")
    country_code = str(country_code).upper() if country_code else None
    started_at = perf_counter()

    rows = await _load_venues(conn)
    plan = _build_venue_merge_plan(rows, country_code=country_code)
    if limit_merges > 0:
        plan = plan[:limit_merges]

    if dry_run:
        return {
            "status": "OK",
            "job_name": "reconcile_venues_identity",
            "records_processed": 0,
            "dry_run": True,
            "summary": {
                "venues_scanned": len(rows),
                "merge_candidates": len(plan),
                "country_code": country_code,
            },
            "merge_plan": plan[:300],
            "generated_at": iso_utc(),
        }

    await conn.execute(text("SELECT pg_advisory_xact_lock(74500126)"))

    merged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in plan:
        if (perf_counter() - started_at) >= max_runtime_seconds:
            skipped.append({"merged": False, "reason": "TIME_BUDGET_EXCEEDED"})
            break
        result = await _merge_venue_one(
            conn,
            source_venue_id=item["source_venue_id"],
            target_venue_id=item["target_venue_id"],
            reason=item["reason"],
        )
        (merged if result.get("merged") else skipped).append(result)

    return {
        "status": "OK",
        "job_name": "reconcile_venues_identity",
        "records_processed": len(merged),
        "dry_run": False,
        "summary": {
            "venues_scanned": len(rows),
            "merge_candidates": len(plan),
            "merged": len(merged),
            "skipped": len(skipped),
            "country_code": country_code,
            "elapsed_seconds": int(perf_counter() - started_at),
            "max_runtime_seconds": max_runtime_seconds,
        },
        "merged_pairs": merged[:500],
        "skipped_pairs": skipped[:200],
        "generated_at": iso_utc(),
    }

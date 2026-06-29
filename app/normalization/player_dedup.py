from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import iso_utc
from app.normalization.player_identity import (
    is_abbreviated_name,
    name_signature,
    name_tokens,
    normalize_identity_name,
    prefer_display_name,
)


@dataclass
class _PlayerRow:
    player_id: str
    slug: str | None
    display_name: str
    normalized_name: str
    birth_date: Any
    nationality_country_code: str | None
    metadata: dict[str, Any]
    created_at: Any
    updated_at: Any
    alias_count: int
    ref_count: int


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _player_score(row: _PlayerRow) -> int:
    score = 0
    if row.birth_date:
        score += 80
    if row.nationality_country_code:
        score += 10

    first_token = name_tokens(row.normalized_name)[0] if name_tokens(row.normalized_name) else ""
    if len(first_token) > 1:
        score += 25
    else:
        score -= 10

    source = str((row.metadata or {}).get("source") or "").upper()
    if source in {"API_FOOTBALL", "FOOTBALL_DATA", "SPORTMONKS"}:
        score += 10

    score += min(int(row.alias_count), 10)
    score += min(int(row.ref_count) * 2, 20)
    return score


async def _load_players(conn: AsyncConnection) -> list[_PlayerRow]:
    rows = await conn.execute(
        text(
            """
            SELECT
              p.player_id::text,
              p.slug,
              p.display_name,
              p.normalized_name,
              p.birth_date,
              p.nationality_country_code,
              p.metadata,
              p.created_at,
              p.updated_at,
              COALESCE(a.alias_count, 0)::int AS alias_count,
              COALESCE(r.ref_count, 0)::int AS ref_count
            FROM players p
            LEFT JOIN (
              SELECT player_id, COUNT(*)::int AS alias_count
              FROM player_aliases
              GROUP BY player_id
            ) a ON a.player_id = p.player_id
            LEFT JOIN (
              SELECT entity_id AS player_id, COUNT(*)::int AS ref_count
              FROM entity_external_refs
              WHERE entity_type = 'PLAYER'
              GROUP BY entity_id
            ) r ON r.player_id = p.player_id
            """
        )
    )
    out: list[_PlayerRow] = []
    for r in rows:
        m = dict(r._mapping)
        out.append(
            _PlayerRow(
                player_id=m["player_id"],
                slug=m.get("slug"),
                display_name=m["display_name"],
                normalized_name=m["normalized_name"],
                birth_date=m.get("birth_date"),
                nationality_country_code=m.get("nationality_country_code"),
                metadata=m.get("metadata") or {},
                created_at=m.get("created_at"),
                updated_at=m.get("updated_at"),
                alias_count=int(m.get("alias_count") or 0),
                ref_count=int(m.get("ref_count") or 0),
            )
        )
    return out


def _build_merge_plan(players: list[_PlayerRow], country_code: str | None = None) -> list[dict[str, str]]:
    eligible = [
        p
        for p in players
        if name_signature(p.normalized_name) is not None
        and (country_code is None or (p.nationality_country_code or "") == country_code)
    ]

    groups: dict[tuple[str, str, str], list[_PlayerRow]] = {}
    for p in eligible:
        sig = name_signature(p.normalized_name)
        if sig is None:
            continue
        key = (p.nationality_country_code or "", sig[0], sig[1])
        groups.setdefault(key, []).append(p)

    plan: list[dict[str, str]] = []

    # Exact normalized_name duplicates.
    exact_groups: dict[tuple[str, str], list[_PlayerRow]] = {}
    for p in eligible:
        exact_groups.setdefault((p.nationality_country_code or "", p.normalized_name), []).append(p)

    planned_sources: set[str] = set()
    for _, rows in exact_groups.items():
        if len(rows) <= 1:
            continue
        master = sorted(rows, key=lambda x: (_player_score(x), x.created_at), reverse=True)[0]
        for row in rows:
            if row.player_id == master.player_id:
                continue
            plan.append({"source_player_id": row.player_id, "target_player_id": master.player_id, "reason": "EXACT_NORMALIZED_NAME"})
            planned_sources.add(row.player_id)

    # Abbreviation to full-name merge when unambiguous.
    for _, rows in groups.items():
        full_rows = [r for r in rows if not is_abbreviated_name(r.normalized_name)]
        abbr_rows = [r for r in rows if is_abbreviated_name(r.normalized_name)]
        if len(full_rows) != 1:
            continue
        master = full_rows[0]
        for abbr in abbr_rows:
            if abbr.player_id == master.player_id or abbr.player_id in planned_sources:
                continue
            plan.append({"source_player_id": abbr.player_id, "target_player_id": master.player_id, "reason": "ABBREVIATED_TO_FULL_UNAMBIGUOUS"})
            planned_sources.add(abbr.player_id)

    return plan


def _count_ambiguous_signatures(players: list[_PlayerRow], country_code: str | None = None) -> int:
    candidates = [
        p
        for p in players
        if name_signature(p.normalized_name) is not None
        and (country_code is None or (p.nationality_country_code or "") == country_code)
    ]
    grouped: dict[tuple[str, str, str], list[_PlayerRow]] = {}
    for row in candidates:
        sig = name_signature(row.normalized_name)
        if sig is None:
            continue
        grouped.setdefault((row.nationality_country_code or "", sig[0], sig[1]), []).append(row)

    ambiguous = 0
    for rows in grouped.values():
        full_names = {r.normalized_name for r in rows if not is_abbreviated_name(r.normalized_name)}
        if len(full_names) > 1:
            ambiguous += 1
    return ambiguous


async def validate_players_identity_consistency(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate identity consistency after weekly player sync without merging records."""
    country_code = payload.get("country_code")
    country_code = str(country_code).upper() if country_code else None
    max_candidates = int(payload.get("max_merge_candidates", 500) or 500)
    max_candidate_ratio = float(payload.get("max_candidate_ratio", 0.03) or 0.03)
    max_ambiguous_signatures = int(payload.get("max_ambiguous_signatures", 200) or 200)

    players = await _load_players(conn)
    plan = _build_merge_plan(players, country_code=country_code)
    ambiguous_signatures = _count_ambiguous_signatures(players, country_code=country_code)
    scoped_players = [
        p
        for p in players
        if country_code is None or (p.nationality_country_code or "") == country_code
    ]
    scoped_count = len(scoped_players)
    candidate_ratio = (len(plan) / scoped_count) if scoped_count else 0.0

    reasons: list[str] = []
    if len(plan) > max_candidates:
        reasons.append("MERGE_CANDIDATES_ABOVE_THRESHOLD")
    if candidate_ratio > max_candidate_ratio:
        reasons.append("MERGE_CANDIDATE_RATIO_ABOVE_THRESHOLD")
    if ambiguous_signatures > max_ambiguous_signatures:
        reasons.append("AMBIGUOUS_SIGNATURES_ABOVE_THRESHOLD")

    status = "WARN" if reasons else "OK"
    return {
        "status": status,
        "job_name": "validate_players_identity_all_leagues",
        "records_processed": scoped_count,
        "warnings": reasons,
        "summary": {
            "players_scanned": len(players),
            "players_in_scope": scoped_count,
            "merge_candidates": len(plan),
            "merge_candidate_ratio": candidate_ratio,
            "ambiguous_signatures": ambiguous_signatures,
            "country_code": country_code,
        },
        "thresholds": {
            "max_merge_candidates": max_candidates,
            "max_candidate_ratio": max_candidate_ratio,
            "max_ambiguous_signatures": max_ambiguous_signatures,
        },
        "merge_plan_preview": plan[:100],
        "generated_at": iso_utc(),
    }


async def _merge_one(conn: AsyncConnection, source_player_id: str, target_player_id: str, reason: str) -> dict[str, Any]:
    if source_player_id == target_player_id:
        return {"merged": False, "reason": "SOURCE_EQUALS_TARGET"}

    source_row_res = await conn.execute(
        text(
            """
            SELECT player_id::text, slug, display_name, normalized_name, birth_date, nationality_country_code, metadata
            FROM players
            WHERE player_id = cast(:player_id as uuid)
            LIMIT 1
            """
        ),
        {"player_id": source_player_id},
    )
    source_row = source_row_res.mappings().first()
    if not source_row:
        return {"merged": False, "reason": "SOURCE_NOT_FOUND"}

    target_row_res = await conn.execute(
        text(
            """
            SELECT player_id::text, slug, display_name, normalized_name, birth_date, nationality_country_code, metadata
            FROM players
            WHERE player_id = cast(:player_id as uuid)
            LIMIT 1
            """
        ),
        {"player_id": target_player_id},
    )
    target_row = target_row_res.mappings().first()
    if not target_row:
        return {"merged": False, "reason": "TARGET_NOT_FOUND"}

    # 1) child tables with uniqueness: upsert into target then delete source rows.
    await conn.execute(
        text(
            """
            INSERT INTO player_aliases
              (player_id, alias, normalized_alias, language_code, source, confidence, created_at, updated_at)
            SELECT
              cast(:target_player_id as uuid),
              alias,
              normalized_alias,
              language_code,
              source,
              confidence,
              created_at,
              now()
            FROM player_aliases
            WHERE player_id = cast(:source_player_id as uuid)
            ON CONFLICT (normalized_alias, source) DO UPDATE SET
              player_id = excluded.player_id,
              alias = excluded.alias,
              confidence = GREATEST(player_aliases.confidence, excluded.confidence),
              updated_at = now()
            """
        ),
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text("DELETE FROM player_aliases WHERE player_id = cast(:source_player_id as uuid)"),
        {"source_player_id": source_player_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO entity_external_refs
              (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name,
               source_url, confidence, is_primary, payload, created_at, updated_at)
            SELECT
              entity_type,
              cast(:target_player_id as uuid),
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
            WHERE entity_type = 'PLAYER'
              AND entity_id = cast(:source_player_id as uuid)
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
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text(
            """
            DELETE FROM entity_external_refs
            WHERE entity_type = 'PLAYER'
              AND entity_id = cast(:source_player_id as uuid)
            """
        ),
        {"source_player_id": source_player_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO competition_rosters
              (competition_season_id, team_id, player_id, shirt_number, position, roster_status, metadata, created_at, updated_at)
            SELECT
              competition_season_id,
              team_id,
              cast(:target_player_id as uuid),
              shirt_number,
              position,
              roster_status,
              metadata,
              created_at,
              now()
            FROM competition_rosters
            WHERE player_id = cast(:source_player_id as uuid)
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
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text("DELETE FROM competition_rosters WHERE player_id = cast(:source_player_id as uuid)"),
        {"source_player_id": source_player_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO team_memberships
              (player_id, team_id, membership_type, valid_from_at, valid_to_at, source, confidence, metadata, created_at, updated_at)
            SELECT
              cast(:target_player_id as uuid),
              team_id,
              membership_type,
              valid_from_at,
              valid_to_at,
              source,
              confidence,
              metadata,
              created_at,
              now()
            FROM team_memberships
            WHERE player_id = cast(:source_player_id as uuid)
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
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text("DELETE FROM team_memberships WHERE player_id = cast(:source_player_id as uuid)"),
        {"source_player_id": source_player_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO match_lineups
              (match_id, team_id, player_id, lineup_role, position, shirt_number, is_captain, source, metadata, created_at, updated_at)
            SELECT
              match_id,
              team_id,
              cast(:target_player_id as uuid),
              lineup_role,
              position,
              shirt_number,
              is_captain,
              source,
              metadata,
              created_at,
              now()
            FROM match_lineups
            WHERE player_id = cast(:source_player_id as uuid)
            ON CONFLICT (match_id, team_id, player_id, source) DO UPDATE SET
              lineup_role = COALESCE(match_lineups.lineup_role, excluded.lineup_role),
              position = COALESCE(match_lineups.position, excluded.position),
              shirt_number = COALESCE(match_lineups.shirt_number, excluded.shirt_number),
              is_captain = match_lineups.is_captain OR excluded.is_captain,
              metadata = match_lineups.metadata || excluded.metadata,
              updated_at = now()
            """
        ),
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text("DELETE FROM match_lineups WHERE player_id = cast(:source_player_id as uuid)"),
        {"source_player_id": source_player_id},
    )

    await conn.execute(
        text(
            """
            INSERT INTO player_match_stats
              (match_id, team_id, player_id, stat_name, stat_value, source, captured_at, payload)
            SELECT
              match_id,
              team_id,
              cast(:target_player_id as uuid),
              stat_name,
              stat_value,
              source,
              captured_at,
              payload
            FROM player_match_stats
            WHERE player_id = cast(:source_player_id as uuid)
            ON CONFLICT (match_id, player_id, stat_name, source) DO UPDATE SET
              stat_value = COALESCE(player_match_stats.stat_value, excluded.stat_value),
              captured_at = LEAST(player_match_stats.captured_at, excluded.captured_at),
              payload = player_match_stats.payload || excluded.payload
            """
        ),
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text("DELETE FROM player_match_stats WHERE player_id = cast(:source_player_id as uuid)"),
        {"source_player_id": source_player_id},
    )

    # 2) direct foreign keys without uniqueness collisions.
    await conn.execute(
        text(
            """
            UPDATE match_events
            SET player_id = cast(:target_player_id as uuid)
            WHERE player_id = cast(:source_player_id as uuid)
            """
        ),
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )
    await conn.execute(
        text(
            """
            UPDATE match_events
            SET related_player_id = cast(:target_player_id as uuid)
            WHERE related_player_id = cast(:source_player_id as uuid)
            """
        ),
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )

    await conn.execute(
        text(
            """
            UPDATE entity_resolution_queue
            SET resolved_entity_id = cast(:target_player_id as uuid)
            WHERE resolved_entity_id = cast(:source_player_id as uuid)
            """
        ),
        {"source_player_id": source_player_id, "target_player_id": target_player_id},
    )

    # 3) enrich target player metadata and best display name.
    target_metadata = dict(target_row.get("metadata") or {})
    source_metadata = dict(source_row.get("metadata") or {})

    merged_from = list(target_metadata.get("merged_player_ids") or [])
    if source_player_id not in merged_from:
        merged_from.append(source_player_id)

    source_slugs = list(target_metadata.get("source_slugs") or [])
    if source_row.get("slug") and source_row["slug"] not in source_slugs:
        source_slugs.append(source_row["slug"])

    merged_sources = list(target_metadata.get("merged_sources") or [])
    src = str(source_metadata.get("source") or "")
    if src and src not in merged_sources:
        merged_sources.append(src)

    merged_metadata = {
        **target_metadata,
        **source_metadata,
        "source_slugs": source_slugs,
        "merged_player_ids": merged_from,
        "merged_sources": merged_sources,
        "last_merge_reason": reason,
        "last_merged_at": datetime.now(timezone.utc).isoformat(),
    }

    display_name = prefer_display_name(target_row["display_name"], source_row["display_name"])
    normalized_name = target_row["normalized_name"]
    if display_name != target_row["display_name"]:
        normalized_name = normalize_identity_name(display_name)

    await conn.execute(
        text(
            """
            UPDATE players
            SET
              display_name = :display_name,
              normalized_name = :normalized_name,
              birth_date = COALESCE(players.birth_date, cast(:birth_date as date)),
              nationality_country_code = COALESCE(players.nationality_country_code, :nationality_country_code),
              metadata = cast(:metadata as jsonb),
              updated_at = now()
            WHERE player_id = cast(:target_player_id as uuid)
            """
        ),
        {
            "target_player_id": target_player_id,
            "display_name": display_name,
            "normalized_name": normalized_name,
            "birth_date": source_row.get("birth_date"),
            "nationality_country_code": source_row.get("nationality_country_code"),
            "metadata": _json(merged_metadata),
        },
    )

    # 4) remove merged source player.
    await conn.execute(
        text("DELETE FROM players WHERE player_id = cast(:source_player_id as uuid)"),
        {"source_player_id": source_player_id},
    )

    return {
        "merged": True,
        "source_player_id": source_player_id,
        "target_player_id": target_player_id,
        "reason": reason,
    }


async def reconcile_players_identity(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Detect and merge duplicated players created from multiple sources.

    Payload options:
    - dry_run: bool (default false)
    - limit_merges: int (default 1000)
    - country_code: optional alpha-2 filter (e.g. "AR")
    """
    dry_run = bool(payload.get("dry_run", False))
    limit_merges = int(payload.get("limit_merges", 1000) or 1000)
    country_code = payload.get("country_code")
    country_code = str(country_code).upper() if country_code else None

    players = await _load_players(conn)
    plan = _build_merge_plan(players, country_code=country_code)
    if limit_merges > 0:
        plan = plan[:limit_merges]

    if dry_run:
        return {
            "status": "OK",
            "job_name": "reconcile_players_identity",
            "records_processed": 0,
            "dry_run": True,
            "summary": {
                "players_scanned": len(players),
                "merge_candidates": len(plan),
                "country_code": country_code,
            },
            "merge_plan": plan[:300],
            "generated_at": iso_utc(),
        }

    merges_done: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Serialize operation with advisory lock to avoid concurrent reconcile executions.
    await conn.execute(text("SELECT pg_advisory_xact_lock(74500123)"))

    for item in plan:
        result = await _merge_one(
            conn,
            source_player_id=item["source_player_id"],
            target_player_id=item["target_player_id"],
            reason=item["reason"],
        )
        if result.get("merged"):
            merges_done.append(result)
        else:
            skipped.append(result)

    return {
        "status": "OK",
        "job_name": "reconcile_players_identity",
        "records_processed": len(merges_done),
        "dry_run": False,
        "summary": {
            "players_scanned": len(players),
            "merge_candidates": len(plan),
            "merged": len(merges_done),
            "skipped": len(skipped),
            "country_code": country_code,
        },
        "merged_pairs": merges_done[:500],
        "skipped_pairs": skipped[:200],
        "generated_at": iso_utc(),
    }

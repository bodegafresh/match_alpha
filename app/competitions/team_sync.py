"""Multi-league teams, squads and players sync via API-Football.

Called by the weekly cron jobs to keep teams and player rosters up to date
for every competition in the catalog that has an API_FOOTBALL external_id.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.clients.api_football_client import ApiFootballClient
from app.competitions.catalog import supported_competitions
from app.core.config import get_settings
from app.core.time import iso_utc
from app.db.repositories.observability import ObservabilityRepository
from app.normalization.team_normalizer import slugify_name

log = logging.getLogger(__name__)

SCHEMA_CANONICAL = "canonical"
SCHEMA_UNKNOWN = "unknown"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFD", str(name or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def _slug(name: str) -> str:
    normalized = slugify_name(str(name or ""))
    return normalized or _norm(name).replace(" ", "-")


def _country_code_or_none(country: str | None) -> str | None:
    value = (country or "").strip().upper()
    if len(value) == 2 and value.isalpha():
        return value
    return None


async def _resolve_country_code(conn: AsyncConnection, country_value: str | None, cache: dict[str, str | None]) -> str | None:
    """Resolve source country strings to canonical ISO alpha-2 code.

    Accepts alpha-2 directly and falls back to countries default_name/names/payload aliases.
    """
    value = (country_value or "").strip()
    if not value:
        return None

    cache_key = _norm(value)
    if cache_key in cache:
        return cache[cache_key]

    direct = _country_code_or_none(value)
    if direct:
        cache[cache_key] = direct
        return direct

    row = await conn.execute(
        text(
            """
            SELECT c.code_alpha2
            FROM countries c
            WHERE lower(c.default_name) = lower(:value)
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_each_text(c.names) n
                    WHERE lower(n.value) = lower(:value)
               )
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(coalesce(c.payload->'aliases', '[]'::jsonb)) a
                    WHERE lower(a.value) = lower(:value)
               )
            LIMIT 1
            """
        ),
        {"value": value},
    )
    resolved = row.scalar_one_or_none()
    cache[cache_key] = resolved
    return resolved


async def _upsert_team_alias(conn: AsyncConnection, team_id: str, alias: str, source: str) -> None:
    normalized_alias = _norm(alias)
    if not normalized_alias:
        return
    await conn.execute(
        text(
            """
            INSERT INTO team_aliases
              (team_id, alias, normalized_alias, source, confidence)
            VALUES
              (cast(:team_id as uuid), :alias, :normalized_alias, :source, 1)
            ON CONFLICT (normalized_alias, source) DO UPDATE SET
              team_id = excluded.team_id,
              alias = excluded.alias,
              confidence = excluded.confidence,
              updated_at = now()
            """
        ),
        {
            "team_id": team_id,
            "alias": alias,
            "normalized_alias": normalized_alias,
            "source": source,
        },
    )


async def _upsert_team_external_ref(
    conn: AsyncConnection,
    team_id: str,
    source: str,
    source_team_id: str,
    source_team_name: str,
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO entity_external_refs
              (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name, confidence, is_primary, payload)
            VALUES
              ('TEAM', cast(:team_id as uuid), :source, 'team', :source_team_id, :source_team_name, 1, true,
               cast(:payload as jsonb))
            ON CONFLICT (entity_type, source, source_entity_id) DO UPDATE SET
              entity_id = excluded.entity_id,
              source_entity_name = excluded.source_entity_name,
              confidence = excluded.confidence,
              payload = entity_external_refs.payload || excluded.payload,
              updated_at = now()
            """
        ),
        {
            "team_id": team_id,
            "source": source,
            "source_team_id": source_team_id,
            "source_team_name": source_team_name,
            "payload": _json({"resolver": "team_sync", "source": source}),
        },
    )


async def _resolve_or_create_team(
    conn: AsyncConnection,
    *,
    source: str,
    source_team_id: str,
    display_name: str,
    team_type: str,
    country_code: str | None,
    metadata: dict[str, Any],
) -> str:
    """Resolve canonical team across source naming variants and create only when needed."""
    # 1) Exact source external ref.
    ext_row = await conn.execute(
        text(
            """
            SELECT entity_id::text
            FROM entity_external_refs
            WHERE entity_type = 'TEAM'
              AND source = :source
              AND source_entity_id = :source_team_id
            LIMIT 1
            """
        ),
        {"source": source, "source_team_id": source_team_id},
    )
    team_id = ext_row.scalar_one_or_none()

    # 2) Fuzzy canonical identity by normalized_name + team_type (+ optional country).
    if not team_id:
        filters = ["normalized_name = :normalized_name", "team_type = cast(:team_type as team_type)"]
        params: dict[str, Any] = {
            "normalized_name": _norm(display_name),
            "team_type": team_type,
        }
        if country_code:
            filters.append("country_code = :country_code")
            params["country_code"] = country_code

        row = await conn.execute(
            text(
                f"""
                SELECT team_id::text
                FROM teams
                WHERE {' AND '.join(filters)}
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            params,
        )
        team_id = row.scalar_one_or_none()

    # 3) Alias-based match.
    if not team_id:
        row = await conn.execute(
            text(
                """
                SELECT ta.team_id::text
                FROM team_aliases ta
                JOIN teams t ON t.team_id = ta.team_id
                WHERE ta.normalized_alias = :normalized_alias
                  AND t.team_type = cast(:team_type as team_type)
                ORDER BY ta.updated_at DESC
                LIMIT 1
                """
            ),
            {
                "normalized_alias": _norm(display_name),
                "team_type": team_type,
            },
        )
        team_id = row.scalar_one_or_none()

    # 4) Create if unresolved.
    if not team_id:
        created = await conn.execute(
            text(
                """
                INSERT INTO teams
                  (slug, team_type, display_name, normalized_name, country_code, metadata)
                VALUES
                  (:slug, cast(:team_type as team_type), :display_name, :normalized_name, :country_code,
                   cast(:metadata as jsonb))
                ON CONFLICT (slug) DO UPDATE SET
                  display_name = excluded.display_name,
                  normalized_name = excluded.normalized_name,
                  team_type = excluded.team_type,
                  country_code = COALESCE(excluded.country_code, teams.country_code),
                  metadata = teams.metadata || excluded.metadata,
                  updated_at = now()
                RETURNING team_id::text
                """
            ),
            {
                "slug": _slug(display_name),
                "team_type": team_type,
                "display_name": display_name,
                "normalized_name": _norm(display_name),
                "country_code": country_code,
                "metadata": _json(metadata),
            },
        )
        team_id = created.scalar_one()
    else:
        await conn.execute(
            text(
                """
                UPDATE teams
                SET display_name = COALESCE(:display_name, display_name),
                    normalized_name = COALESCE(:normalized_name, normalized_name),
                    country_code = COALESCE(:country_code, country_code),
                    metadata = teams.metadata || cast(:metadata as jsonb),
                    updated_at = now()
                WHERE team_id = cast(:team_id as uuid)
                """
            ),
            {
                "team_id": team_id,
                "display_name": display_name,
                "normalized_name": _norm(display_name),
                "country_code": country_code,
                "metadata": _json(metadata),
            },
        )

    await _upsert_team_alias(conn, team_id, display_name, source)
    await _upsert_team_external_ref(conn, team_id, source, source_team_id, display_name)
    return team_id


async def _table_exists(conn: AsyncConnection, table_name: str) -> bool:
    row = await conn.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema = 'public'
                AND table_name = :table_name
            )
            """
        ),
        {"table_name": table_name},
    )
    return bool(row.scalar_one())


async def _detect_sync_schema(conn: AsyncConnection) -> str:
    has_canonical = await _table_exists(conn, "competition_team_entries") and await _table_exists(conn, "competition_rosters")
    if has_canonical:
        return SCHEMA_CANONICAL

    return SCHEMA_UNKNOWN


async def _record_sync_event(conn: AsyncConnection, severity: str, check_type: str, message: str, payload: dict[str, Any]) -> None:
    try:
        obs = ObservabilityRepository(conn)
        await obs.data_quality_event("ANALYTICS", severity, check_type, message, payload)
    except Exception:
        # Observability should never break sync execution.
        log.exception("team_sync: failed to write data_quality_event check_type=%s", check_type)


async def sync_teams_for_all_leagues(conn: AsyncConnection) -> dict[str, Any]:
    """Upsert teams for every catalog entry with an API_FOOTBALL external_id (canonical schema only)."""
    settings = get_settings()
    if not settings.api_football_key:
        return {
            "status": "WARN",
            "job_name": "sync_all_leagues_teams",
            "records_processed": 0,
            "errors": ["API_FOOTBALL_KEY is not configured."],
            "schema_mode": SCHEMA_UNKNOWN,
            "generated_at": iso_utc(),
        }

    client = ApiFootballClient()
    total_teams = 0
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    league_stats: list[dict[str, Any]] = []
    country_cache: dict[str, str | None] = {}
    schema_mode = await _detect_sync_schema(conn)

    if schema_mode != SCHEMA_CANONICAL:
        return {
            "status": "WARN",
            "job_name": "sync_all_leagues_teams",
            "records_processed": 0,
            "errors": ["Canonical sync schema is not available (competition_team_entries + competition_rosters required)."],
            "schema_mode": schema_mode,
            "generated_at": iso_utc(),
        }

    for entry in supported_competitions():
        league_id = entry.source.external_ids.get("API_FOOTBALL")
        if not league_id:
            skipped.append({"league": entry.slug, "reason": "MISSING_API_FOOTBALL_EXTERNAL_ID"})
            continue

        # Derive season year from the entry's start date (e.g. "2026" from "2026-06-01")
        season_year = (entry.starts_at or "")[:4]
        if not season_year.isdigit():
            skipped.append({"league": entry.slug, "reason": "INVALID_SEASON_YEAR"})
            continue

        try:
            data = await client.teams(league=int(league_id), season=int(season_year))
            teams_raw = (data.get("response") or [])
        except Exception as exc:
            log.warning("team_sync: API-Football teams failed league=%s season=%s err=%s", league_id, season_year, exc)
            errors.append(f"{entry.slug}: {exc}")
            league_stats.append(
                {
                    "league": entry.slug,
                    "status": "ERROR",
                    "teams_processed": 0,
                    "error": str(exc),
                }
            )
            await _record_sync_event(
                conn,
                "ERROR",
                "TEAM_SYNC_LEAGUE_ERROR",
                f"team sync API failed for {entry.slug}",
                {"league": entry.slug, "league_id": str(league_id), "season_year": season_year, "error": str(exc)},
            )
            continue

        # Competition season must already exist from catalog seeding.
        cs_row = await conn.execute(
            text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
            {"slug": entry.slug},
        )
        cs = cs_row.fetchone()
        if not cs:
            log.debug("team_sync: season not seeded yet slug=%s, skipping", entry.slug)
            skipped.append({"league": entry.slug, "reason": "SEASON_NOT_FOUND"})
            continue
        competition_season_id = cs[0]
        league_processed = 0

        for item in teams_raw:
            team_info = item.get("team") or {}
            venue_info = item.get("venue") or {}
            if not team_info.get("id"):
                continue

            team_slug = _slug(team_info.get("name", ""))
            external_id = str(team_info["id"])

            try:
                team_name = team_info.get("name", team_slug)
                team_type = "NATIONAL_TEAM" if bool(team_info.get("national", False)) else "CLUB"
                country_code = await _resolve_country_code(conn, team_info.get("country"), country_cache)
                team_id = await _resolve_or_create_team(
                    conn,
                    source="API_FOOTBALL",
                    source_team_id=external_id,
                    display_name=team_name,
                    team_type=team_type,
                    country_code=country_code,
                    metadata={
                        "api_football_id": external_id,
                        "country": team_info.get("country"),
                        "founded_year": team_info.get("founded"),
                        "stadium_name": venue_info.get("name"),
                        "stadium_capacity": venue_info.get("capacity"),
                        "logo_url": team_info.get("logo"),
                        "ingestion_source": "API_FOOTBALL",
                    },
                )

                # Link team to competition_season
                await conn.execute(
                    text(
                        """
                        INSERT INTO competition_team_entries
                          (competition_season_id, team_id, entry_status, metadata)
                        VALUES
                          (cast(:cs_id as uuid), cast(:team_id as uuid), 'ACTIVE', cast(:metadata as jsonb))
                        ON CONFLICT (competition_season_id, team_id) DO UPDATE SET
                          metadata = competition_team_entries.metadata || excluded.metadata,
                          updated_at = now()
                        """
                    ),
                    {
                        "cs_id": competition_season_id,
                        "team_id": team_id,
                        "metadata": _json({"source": "API_FOOTBALL", "external_id": external_id}),
                    },
                )
                total_teams += 1
                league_processed += 1
            except Exception as exc:
                log.warning("team_sync: upsert failed slug=%s team=%s err=%s", entry.slug, team_info.get("name"), exc)
                errors.append(f"{entry.slug}/{team_info.get('name')}: {exc}")

        league_status = "OK" if league_processed > 0 else "WARN"
        league_stats.append(
            {
                "league": entry.slug,
                "status": league_status,
                "teams_processed": league_processed,
                "teams_received": len(teams_raw),
            }
        )
        await _record_sync_event(
            conn,
            "INFO" if league_status == "OK" else "WARN",
            "TEAM_SYNC_LEAGUE_RESULT",
            f"team sync completed for {entry.slug}",
            {
                "league": entry.slug,
                "teams_processed": league_processed,
                "teams_received": len(teams_raw),
                "status": league_status,
            },
        )

    status = "WARN" if errors else "OK"
    summary = {
        "eligible_leagues": len([c for c in supported_competitions() if c.source.external_ids.get("API_FOOTBALL")]),
        "processed_leagues": len([s for s in league_stats if s.get("status") == "OK"]),
        "warn_leagues": len([s for s in league_stats if s.get("status") == "WARN"]),
        "error_leagues": len([s for s in league_stats if s.get("status") == "ERROR"]),
        "skipped_leagues": len(skipped),
    }
    return {
        "status": status,
        "job_name": "sync_all_leagues_teams",
        "schema_mode": schema_mode,
        "records_processed": total_teams,
        "summary": summary,
        "league_stats": league_stats[:200],
        "skipped": skipped[:200],
        "errors": errors[:20],
        "generated_at": iso_utc(),
    }


async def sync_players_for_all_leagues(conn: AsyncConnection) -> dict[str, Any]:
    """Upsert players and competition rosters for every catalog entry with API_FOOTBALL.

    Players API is paginated. We iterate pages until paging.current == paging.total.
    This is rate-limit aware: if no API key is configured the loop is skipped gracefully.
    """
    settings = get_settings()
    if not settings.api_football_key:
        return {
            "status": "WARN",
            "job_name": "sync_all_leagues_players",
            "records_processed": 0,
            "errors": ["API_FOOTBALL_KEY is not configured."],
            "schema_mode": SCHEMA_UNKNOWN,
            "generated_at": iso_utc(),
        }

    async def _upsert_player_alias(player_id: str, alias: str, source: str) -> None:
        normalized_alias = _norm(alias)
        if not normalized_alias:
            return
        await conn.execute(
            text(
                """
                INSERT INTO player_aliases
                  (player_id, alias, normalized_alias, source, confidence)
                VALUES
                  (cast(:player_id as uuid), :alias, :normalized_alias, :source, 1)
                ON CONFLICT (normalized_alias, source) DO UPDATE SET
                  player_id = excluded.player_id,
                  alias = excluded.alias,
                  confidence = excluded.confidence,
                  updated_at = now()
                """
            ),
            {
                "player_id": player_id,
                "alias": alias,
                "normalized_alias": normalized_alias,
                "source": source,
            },
        )

    async def _upsert_player_external_ref(player_id: str, source: str, source_player_id: str, source_player_name: str) -> None:
        await conn.execute(
            text(
                """
                INSERT INTO entity_external_refs
                  (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name, confidence, is_primary, payload)
                VALUES
                  ('PLAYER', cast(:player_id as uuid), :source, 'player', :source_player_id, :source_player_name, 1, true,
                   cast(:payload as jsonb))
                ON CONFLICT (entity_type, source, source_entity_id) DO UPDATE SET
                  entity_id = excluded.entity_id,
                  source_entity_name = excluded.source_entity_name,
                  confidence = excluded.confidence,
                  payload = entity_external_refs.payload || excluded.payload,
                  updated_at = now()
                """
            ),
            {
                "player_id": player_id,
                "source": source,
                "source_player_id": source_player_id,
                "source_player_name": source_player_name,
                "payload": _json({"resolver": "player_sync", "source": source}),
            },
        )

    async def _resolve_or_create_player(
        *,
        source: str,
        source_player_id: str,
        display_name: str,
        birth_date: str | None,
        nationality_country_code: str | None,
        metadata: dict[str, Any],
    ) -> str:
        # 1) Exact source external ref.
        ext = await conn.execute(
            text(
                """
                SELECT entity_id::text
                FROM entity_external_refs
                WHERE entity_type = 'PLAYER'
                  AND source = :source
                  AND source_entity_id = :source_player_id
                LIMIT 1
                """
            ),
            {"source": source, "source_player_id": source_player_id},
        )
        player_id = ext.scalar_one_or_none()

        # 2) Identity by normalized_name + birth_date + nationality.
        if not player_id and birth_date:
            row = await conn.execute(
                text(
                    """
                    SELECT player_id::text
                    FROM players
                    WHERE normalized_name = :normalized_name
                      AND birth_date = cast(:birth_date as date)
                      AND (
                        :nationality_country_code is null
                        OR nationality_country_code = :nationality_country_code
                      )
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "normalized_name": _norm(display_name),
                    "birth_date": birth_date,
                    "nationality_country_code": nationality_country_code,
                },
            )
            player_id = row.scalar_one_or_none()

        # 3) Alias fallback.
        if not player_id:
            row = await conn.execute(
                text(
                    """
                    SELECT pa.player_id::text
                    FROM player_aliases pa
                    WHERE pa.normalized_alias = :normalized_alias
                    ORDER BY pa.updated_at DESC
                    LIMIT 1
                    """
                ),
                {"normalized_alias": _norm(display_name)},
            )
            player_id = row.scalar_one_or_none()

        if not player_id:
            slug = f"{_slug(display_name)}-{source.lower()}-{source_player_id}"[:180]
            created = await conn.execute(
                text(
                    """
                    INSERT INTO players
                      (slug, display_name, normalized_name, birth_date, nationality_country_code, metadata)
                    VALUES
                      (:slug, :display_name, :normalized_name, cast(:birth_date as date), :nationality_country_code,
                       cast(:metadata as jsonb))
                    RETURNING player_id::text
                    """
                ),
                {
                    "slug": slug,
                    "display_name": display_name,
                    "normalized_name": _norm(display_name),
                    "birth_date": birth_date,
                    "nationality_country_code": nationality_country_code,
                    "metadata": _json(metadata),
                },
            )
            player_id = created.scalar_one()
        else:
            await conn.execute(
                text(
                    """
                    UPDATE players
                    SET display_name = COALESCE(:display_name, display_name),
                        normalized_name = COALESCE(:normalized_name, normalized_name),
                        birth_date = COALESCE(cast(:birth_date as date), birth_date),
                        nationality_country_code = COALESCE(:nationality_country_code, nationality_country_code),
                        metadata = players.metadata || cast(:metadata as jsonb),
                        updated_at = now()
                    WHERE player_id = cast(:player_id as uuid)
                    """
                ),
                {
                    "player_id": player_id,
                    "display_name": display_name,
                    "normalized_name": _norm(display_name),
                    "birth_date": birth_date,
                    "nationality_country_code": nationality_country_code,
                    "metadata": _json(metadata),
                },
            )

        await _upsert_player_alias(player_id, display_name, source)
        await _upsert_player_external_ref(player_id, source, source_player_id, display_name)
        return player_id

    async def _resolve_team_id_for_api_football(team_external_id: str) -> str | None:
        ref_row = await conn.execute(
            text(
                """
                SELECT entity_id::text
                FROM entity_external_refs
                WHERE entity_type = 'TEAM'
                  AND source = 'API_FOOTBALL'
                  AND source_entity_id = :team_external_id
                LIMIT 1
                """
            ),
            {"team_external_id": team_external_id},
        )
        team_id = ref_row.scalar_one_or_none()
        if team_id:
            return team_id

        legacy_row = await conn.execute(
            text(
                """
                SELECT t.team_id::text
                FROM teams t
                JOIN competition_team_entries cte ON cte.team_id = t.team_id
                WHERE cte.competition_season_id = cast(:cs_id as uuid)
                  AND cte.metadata->>'external_id' = :team_external_id
                LIMIT 1
                """
            ),
            {"cs_id": competition_season_id, "team_external_id": team_external_id},
        )
        return legacy_row.scalar_one_or_none()

    async def _upsert_team_membership(player_id: str, team_id: str, source: str) -> None:
        await conn.execute(
            text(
                """
                UPDATE team_memberships
                SET valid_to_at = now(),
                    updated_at = now(),
                    metadata = team_memberships.metadata || '{"ended_by":"player_sync"}'::jsonb
                WHERE player_id = cast(:player_id as uuid)
                  AND source = :source
                  AND membership_type = 'CLUB'
                  AND valid_to_at IS NULL
                  AND team_id <> cast(:team_id as uuid)
                """
            ),
            {"player_id": player_id, "team_id": team_id, "source": source},
        )

        await conn.execute(
            text(
                """
                INSERT INTO team_memberships
                  (player_id, team_id, membership_type, valid_from_at, valid_to_at, source, confidence, metadata)
                VALUES
                  (cast(:player_id as uuid), cast(:team_id as uuid), 'CLUB', now(), null, :source, 1,
                   cast(:metadata as jsonb))
                ON CONFLICT (player_id, team_id, membership_type, source) DO UPDATE SET
                  valid_to_at = null,
                  confidence = excluded.confidence,
                  metadata = team_memberships.metadata || excluded.metadata,
                  updated_at = now()
                """
            ),
            {
                "player_id": player_id,
                "team_id": team_id,
                "source": source,
                "metadata": _json({"updated_by": "player_sync"}),
            },
        )

    async def _reconcile_removed_rosters(active_pairs: list[dict[str, str]]) -> int:
        if not active_pairs:
            result = await conn.execute(
                text(
                    """
                    UPDATE competition_rosters cr
                    SET roster_status = 'CUT',
                        updated_at = now(),
                        metadata = cr.metadata || '{"deactivated_by":"player_sync","source":"API_FOOTBALL"}'::jsonb
                    WHERE cr.competition_season_id = cast(:cs_id as uuid)
                      AND coalesce(cr.metadata->>'source','') = 'API_FOOTBALL'
                      AND cr.roster_status in ('ACTIVE', 'CALLED_UP', 'UNKNOWN')
                    """
                ),
                {"cs_id": competition_season_id},
            )
            return int(result.rowcount or 0)

        result = await conn.execute(
            text(
                """
                WITH active_pairs AS (
                  SELECT
                    cast(item->>'team_id' as uuid) AS team_id,
                    cast(item->>'player_id' as uuid) AS player_id
                  FROM jsonb_array_elements(cast(:active_pairs as jsonb)) item
                )
                UPDATE competition_rosters cr
                SET roster_status = 'CUT',
                    updated_at = now(),
                    metadata = cr.metadata || '{"deactivated_by":"player_sync","source":"API_FOOTBALL"}'::jsonb
                WHERE cr.competition_season_id = cast(:cs_id as uuid)
                  AND coalesce(cr.metadata->>'source','') = 'API_FOOTBALL'
                  AND cr.roster_status in ('ACTIVE', 'CALLED_UP', 'UNKNOWN')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM active_pairs a
                    WHERE a.team_id = cr.team_id
                      AND a.player_id = cr.player_id
                  )
                """
            ),
            {"cs_id": competition_season_id, "active_pairs": _json(active_pairs)},
        )
        return int(result.rowcount or 0)

    client = ApiFootballClient()
    total_players = 0
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    league_stats: list[dict[str, Any]] = []
    country_cache: dict[str, str | None] = {}
    schema_mode = await _detect_sync_schema(conn)

    if schema_mode != SCHEMA_CANONICAL:
        return {
            "status": "WARN",
            "job_name": "sync_all_leagues_players",
            "records_processed": 0,
            "errors": ["Canonical sync schema is not available (competition_team_entries + competition_rosters required)."],
            "schema_mode": schema_mode,
            "generated_at": iso_utc(),
        }

    for entry in supported_competitions():
        league_id = entry.source.external_ids.get("API_FOOTBALL")
        if not league_id:
            skipped.append({"league": entry.slug, "reason": "MISSING_API_FOOTBALL_EXTERNAL_ID"})
            continue
        if "players" not in (entry.source.capabilities.get("API_FOOTBALL") or []):
            skipped.append({"league": entry.slug, "reason": "PLAYERS_CAPABILITY_NOT_ENABLED"})
            continue

        season_year = (entry.starts_at or "")[:4]
        if not season_year.isdigit():
            skipped.append({"league": entry.slug, "reason": "INVALID_SEASON_YEAR"})
            continue

        # Get competition_season_id
        cs_row = await conn.execute(
            text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
            {"slug": entry.slug},
        )
        cs = cs_row.fetchone()
        if not cs:
            skipped.append({"league": entry.slug, "reason": "SEASON_NOT_FOUND"})
            continue
        competition_season_id = cs[0]
        league_processed = 0
        league_errors = 0
        pages_processed = 0
        active_pairs: list[dict[str, str]] = []

        page = 1
        while True:
            try:
                data = await client.players(league=int(league_id), season=int(season_year), page=page)
            except Exception as exc:
                log.warning("player_sync: API failed league=%s page=%s err=%s", league_id, page, exc)
                errors.append(f"{entry.slug} page={page}: {exc}")
                league_errors += 1
                await _record_sync_event(
                    conn,
                    "ERROR",
                    "PLAYER_SYNC_LEAGUE_ERROR",
                    f"player sync API failed for {entry.slug}",
                    {"league": entry.slug, "league_id": str(league_id), "season_year": season_year, "page": page, "error": str(exc)},
                )
                break

            items = data.get("response") or []
            paging = data.get("paging") or {}
            pages_processed += 1

            for item in items:
                player_info = item.get("player") or {}
                statistics = item.get("statistics") or [{}]
                stat = statistics[0] if statistics else {}
                team_info = (stat.get("team") or {})

                if not player_info.get("id"):
                    continue

                external_id = str(player_info["id"])

                try:
                    player_name = (
                        player_info.get("name")
                        or f"{player_info.get('firstname','')} {player_info.get('lastname','')}".strip()
                        or f"player-{external_id}"
                    )
                    player_id = await _resolve_or_create_player(
                        source="API_FOOTBALL",
                        source_player_id=external_id,
                        display_name=player_name,
                        birth_date=player_info.get("birth", {}).get("date"),
                        nationality_country_code=await _resolve_country_code(conn, player_info.get("nationality"), country_cache),
                        metadata={
                            "api_football_id": external_id,
                            "position": player_info.get("position"),
                            "number": player_info.get("number"),
                            "photo_url": player_info.get("photo"),
                            "nationality": player_info.get("nationality"),
                            "ingestion_source": "API_FOOTBALL",
                        },
                    )

                    # Roster/membership links.
                    if team_info.get("id"):
                        team_ext = str(team_info["id"])
                        team_id = await _resolve_team_id_for_api_football(team_ext)
                        if team_id:
                            await conn.execute(
                                text(
                                    """
                                    INSERT INTO competition_rosters
                                      (competition_season_id, team_id, player_id, shirt_number,
                                       position, roster_status, metadata)
                                    VALUES
                                      (cast(:cs_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid),
                                       :number, :position, 'ACTIVE', cast(:metadata as jsonb))
                                    ON CONFLICT (competition_season_id, team_id, player_id) DO UPDATE SET
                                      position = COALESCE(excluded.position, competition_rosters.position),
                                      shirt_number = COALESCE(excluded.shirt_number, competition_rosters.shirt_number),
                                      roster_status = 'ACTIVE',
                                      metadata = competition_rosters.metadata || excluded.metadata,
                                      updated_at = now()
                                    """
                                ),
                                {
                                    "cs_id": competition_season_id,
                                    "team_id": team_id,
                                    "player_id": player_id,
                                    "position": player_info.get("position"),
                                    "number": player_info.get("number"),
                                    "metadata": _json(
                                        {
                                            "source": "API_FOOTBALL",
                                            "team_external_id": team_ext,
                                            "league": entry.slug,
                                        }
                                    ),
                                },
                            )

                            await _upsert_team_membership(player_id, team_id, "API_FOOTBALL")
                            active_pairs.append({"team_id": team_id, "player_id": player_id})

                    total_players += 1
                    league_processed += 1
                except Exception as exc:
                    log.warning("player_sync: upsert failed slug=%s player=%s err=%s", entry.slug, player_info.get("name"), exc)
                    errors.append(f"{entry.slug}/{player_info.get('name')}: {exc}")
                    league_errors += 1

            if not items or paging.get("current") >= paging.get("total", 1):
                break
            page += 1

        deactivated_rosters = await _reconcile_removed_rosters(active_pairs)

        if league_errors > 0:
            league_status = "WARN"
        elif league_processed > 0:
            league_status = "OK"
        else:
            league_status = "WARN"

        league_stats.append(
            {
                "league": entry.slug,
                "status": league_status,
                "players_processed": league_processed,
                "pages_processed": pages_processed,
                "errors": league_errors,
                "rosters_deactivated": deactivated_rosters,
            }
        )
        await _record_sync_event(
            conn,
            "INFO" if league_status == "OK" else "WARN",
            "PLAYER_SYNC_LEAGUE_RESULT",
            f"player sync completed for {entry.slug}",
            {
                "league": entry.slug,
                "players_processed": league_processed,
                "pages_processed": pages_processed,
                "errors": league_errors,
                "rosters_deactivated": deactivated_rosters,
                "status": league_status,
            },
        )

    status = "WARN" if errors else "OK"
    summary = {
        "eligible_leagues": len(
            [
                c
                for c in supported_competitions()
                if c.source.external_ids.get("API_FOOTBALL")
                and "players" in (c.source.capabilities.get("API_FOOTBALL") or [])
            ]
        ),
        "processed_leagues": len([s for s in league_stats if s.get("status") == "OK"]),
        "warn_leagues": len([s for s in league_stats if s.get("status") == "WARN"]),
        "error_leagues": len([s for s in league_stats if s.get("status") == "ERROR"]),
        "skipped_leagues": len(skipped),
    }
    return {
        "status": status,
        "job_name": "sync_all_leagues_players",
        "schema_mode": schema_mode,
        "records_processed": total_players,
        "summary": summary,
        "league_stats": league_stats[:200],
        "skipped": skipped[:200],
        "errors": errors[:20],
        "generated_at": iso_utc(),
    }


async def validate_sync_coverage_for_all_leagues(conn: AsyncConnection, min_players_per_team: int = 11) -> dict[str, Any]:
    """Validate post-cron team/player coverage by league for canonical schema.

    Checks by competition season:
    - Team entries present.
    - Team entries with external_id in metadata.
    - Teams with fewer than min players in competition_rosters.
    - Orphan rosters (team in rosters not present in team entries for same season).
    """
    schema_mode = await _detect_sync_schema(conn)
    if schema_mode != SCHEMA_CANONICAL:
        return {
            "status": "WARN",
            "job_name": "validate_sync_coverage_all_leagues",
            "records_processed": 0,
            "errors": ["Canonical sync schema is not available (competition_team_entries + competition_rosters required)."],
            "schema_mode": schema_mode,
            "generated_at": iso_utc(),
        }

    evaluated = 0
    skipped: list[dict[str, str]] = []
    league_stats: list[dict[str, Any]] = []

    for entry in supported_competitions():
        league_id = entry.source.external_ids.get("API_FOOTBALL")
        if not league_id:
            skipped.append({"league": entry.slug, "reason": "MISSING_API_FOOTBALL_EXTERNAL_ID"})
            continue

        cs_row = await conn.execute(
            text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
            {"slug": entry.slug},
        )
        cs = cs_row.fetchone()
        if not cs:
            skipped.append({"league": entry.slug, "reason": "SEASON_NOT_FOUND"})
            continue
        competition_season_id = cs[0]

        counts_row = await conn.execute(
            text(
                """
                SELECT
                  COUNT(*)::int AS teams_total,
                  COUNT(*) FILTER (WHERE COALESCE(cte.metadata->>'external_id', '') <> '')::int AS teams_with_external_id,
                  COUNT(*) FILTER (WHERE COALESCE(roster.players_count, 0) >= :min_players)::int AS teams_with_min_players,
                  COUNT(*) FILTER (WHERE COALESCE(roster.players_count, 0) < :min_players)::int AS teams_below_min_players
                FROM competition_team_entries cte
                LEFT JOIN (
                  SELECT competition_season_id, team_id, COUNT(*)::int AS players_count
                  FROM competition_rosters
                  GROUP BY competition_season_id, team_id
                ) roster
                  ON roster.competition_season_id = cte.competition_season_id
                 AND roster.team_id = cte.team_id
                WHERE cte.competition_season_id = cast(:cs_id as uuid)
                """
            ),
            {"cs_id": competition_season_id, "min_players": min_players_per_team},
        )
        counts = dict(counts_row.fetchone()._mapping)

        orphan_row = await conn.execute(
            text(
                """
                SELECT COUNT(*)::int AS orphan_rosters
                FROM competition_rosters cr
                LEFT JOIN competition_team_entries cte
                  ON cte.competition_season_id = cr.competition_season_id
                 AND cte.team_id = cr.team_id
                WHERE cr.competition_season_id = cast(:cs_id as uuid)
                  AND cte.competition_team_entry_id IS NULL
                """
            ),
            {"cs_id": competition_season_id},
        )
        orphan_rosters = int(orphan_row.scalar_one())

        if counts["teams_total"] <= 0:
            league_status = "WARN"
            reason = "NO_TEAMS"
        elif counts["teams_with_external_id"] < counts["teams_total"]:
            league_status = "WARN"
            reason = "MISSING_EXTERNAL_IDS"
        elif counts["teams_below_min_players"] > 0:
            league_status = "WARN"
            reason = "TEAMS_BELOW_MIN_PLAYERS"
        elif orphan_rosters > 0:
            league_status = "WARN"
            reason = "ORPHAN_ROSTERS"
        else:
            league_status = "OK"
            reason = "HEALTHY"

        evaluated += 1
        result_item = {
            "league": entry.slug,
            "status": league_status,
            "reason": reason,
            "teams_total": int(counts["teams_total"]),
            "teams_with_external_id": int(counts["teams_with_external_id"]),
            "teams_with_min_players": int(counts["teams_with_min_players"]),
            "teams_below_min_players": int(counts["teams_below_min_players"]),
            "orphan_rosters": orphan_rosters,
            "min_players_per_team": min_players_per_team,
        }
        league_stats.append(result_item)

        await _record_sync_event(
            conn,
            "INFO" if league_status == "OK" else "WARN",
            "SYNC_COVERAGE_LEAGUE_RESULT",
            f"sync coverage validation for {entry.slug}",
            result_item,
        )

    status = "WARN" if any(item["status"] != "OK" for item in league_stats) else "OK"
    summary = {
        "evaluated_leagues": evaluated,
        "ok_leagues": len([s for s in league_stats if s.get("status") == "OK"]),
        "warn_leagues": len([s for s in league_stats if s.get("status") == "WARN"]),
        "skipped_leagues": len(skipped),
        "min_players_per_team": min_players_per_team,
    }
    return {
        "status": status,
        "job_name": "validate_sync_coverage_all_leagues",
        "schema_mode": schema_mode,
        "records_processed": evaluated,
        "summary": summary,
        "league_stats": league_stats[:200],
        "skipped": skipped[:200],
        "errors": [],
        "generated_at": iso_utc(),
    }

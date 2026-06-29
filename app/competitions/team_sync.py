"""Multi-league teams, squads and players sync with canonical source fallback.

Called by the weekly cron jobs to keep teams and player rosters up to date
for every competition in the catalog with supported external ids.
"""
from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.clients.api_football_client import ApiFootballClient
from app.clients.football_data_client import FootballDataClient
from app.clients.sportmonks_client import SportmonksClient
from app.competitions.catalog import supported_competitions
from app.competitions.service import seed_competition_catalog
from app.core.config import get_settings
from app.core.time import iso_utc
from app.db.repositories.observability import ObservabilityRepository
from app.normalization.player_identity import normalize_identity_name
from app.normalization.team_normalizer import slugify_name

log = logging.getLogger(__name__)

SCHEMA_CANONICAL = "canonical"
SCHEMA_UNKNOWN = "unknown"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _norm(name: str) -> str:
    return normalize_identity_name(name)


def _slug(name: str) -> str:
    normalized = slugify_name(str(name or ""))
    return normalized or _norm(name).replace(" ", "-")


def _country_code_or_none(country: str | None) -> str | None:
    value = (country or "").strip().upper()
    if len(value) == 2 and value.isalpha():
        return value
    return None


def _sportmonks_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    node = payload.get(key)
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


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

        # 2.1) Relaxed canonical identity by normalized_name + country_code.
        # This avoids duplicates when providers disagree on team_type for the same national side.
        if not team_id and country_code:
                row = await conn.execute(
                        text(
                                """
                                SELECT team_id::text
                                FROM teams
                                WHERE normalized_name = :normalized_name
                                    AND country_code = :country_code
                                ORDER BY
                                    CASE WHEN team_type = cast(:team_type as team_type) THEN 0 ELSE 1 END,
                                    updated_at DESC
                                LIMIT 1
                                """
                        ),
                        {
                                "normalized_name": _norm(display_name),
                                "country_code": country_code,
                                "team_type": team_type,
                        },
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

        # 3.1) Relaxed alias-based match with country scope, regardless of team_type.
        if not team_id and country_code:
                row = await conn.execute(
                        text(
                                """
                                SELECT ta.team_id::text
                                FROM team_aliases ta
                                JOIN teams t ON t.team_id = ta.team_id
                                WHERE ta.normalized_alias = :normalized_alias
                                    AND t.country_code = :country_code
                                ORDER BY
                                    CASE WHEN t.team_type = cast(:team_type as team_type) THEN 0 ELSE 1 END,
                                    ta.updated_at DESC
                                LIMIT 1
                                """
                        ),
                        {
                                "normalized_alias": _norm(display_name),
                                "country_code": country_code,
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


async def _api_football_calls_today(conn: AsyncConnection) -> int:
    row = await conn.execute(
        text(
            """
            SELECT COUNT(*)::int
            FROM raw_api_calls
            WHERE source = 'API_FOOTBALL'
              AND called_at >= date_trunc('day', now())
              AND called_at < date_trunc('day', now()) + interval '1 day'
            """
        )
    )
    return int(row.scalar_one() or 0)


async def _api_football_has_budget(conn: AsyncConnection, settings: Any, reserve: int = 0) -> bool:
    used = await _api_football_calls_today(conn)
    return used + max(reserve, 0) < int(settings.api_football_daily_budget)


async def _record_api_call(
    conn: AsyncConnection,
    *,
    source: str,
    endpoint: str,
    request_hash: str,
    request_payload: dict[str, Any],
    response_status: int,
    response_hash: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        await conn.execute(
            text(
                """
                INSERT INTO raw_api_calls
                  (source, endpoint, request_hash, request_payload, response_status, response_hash, latency_ms, payload)
                VALUES
                                    (:source, :endpoint, :request_hash, cast(:request_payload as jsonb), :response_status,
                   :response_hash, null, cast(:payload as jsonb))
                """
            ),
            {
                                "source": source,
                "endpoint": endpoint,
                "request_hash": request_hash,
                "request_payload": _json(request_payload),
                "response_status": response_status,
                "response_hash": response_hash,
                "payload": _json(payload or {}),
            },
        )
    except Exception:
        # Quota telemetry must not fail ingestion.
        log.exception("team_sync: failed to persist raw_api_calls endpoint=%s", endpoint)


async def sync_teams_for_all_leagues(conn: AsyncConnection) -> dict[str, Any]:
    """Upsert teams for every catalog entry using source priority (canonical schema only)."""
    settings = get_settings()
    if not settings.api_football_key and not settings.football_data_token and not settings.sportmonks_api_token:
        return {
            "status": "WARN",
            "job_name": "sync_all_leagues_teams",
            "records_processed": 0,
            "errors": ["At least one team source credential is required (API_FOOTBALL_KEY, FOOTBALL_DATA_TOKEN or SPORTMONKS_API_TOKEN)."],
            "schema_mode": SCHEMA_UNKNOWN,
            "generated_at": iso_utc(),
        }

    api_football_client = ApiFootballClient() if settings.api_football_key else None
    football_data_client = FootballDataClient() if settings.football_data_token else None
    sportmonks_client = SportmonksClient() if settings.sportmonks_api_token else None
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
        # Ensure the season exists for every catalog entry before syncing source data.
        await seed_competition_catalog(conn, entry.slug)
        league_api_football_id = entry.source.external_ids.get("API_FOOTBALL")
        league_football_data_code = entry.source.external_ids.get("FOOTBALL_DATA")
        has_sportmonks_capability = "teams" in (entry.source.capabilities.get("SPORTMONKS") or [])
        if not league_api_football_id and not league_football_data_code and not has_sportmonks_capability:
            skipped.append({"league": entry.slug, "reason": "MISSING_SUPPORTED_EXTERNAL_ID"})
            continue

        # Derive season year from the entry's start date (e.g. "2026" from "2026-06-01")
        season_year = (entry.starts_at or "")[:4]
        if not season_year.isdigit():
            skipped.append({"league": entry.slug, "reason": "INVALID_SEASON_YEAR"})
            continue

        selected_source: str | None = None
        teams_rows: list[dict[str, Any]] = []
        source_errors: list[str] = []
        source_attempts: list[dict[str, str]] = []

        if league_api_football_id and api_football_client:
            if not await _api_football_has_budget(conn, settings):
                source_errors.append("API_FOOTBALL_DAILY_BUDGET_EXHAUSTED")
                source_attempts.append({"source": "API_FOOTBALL", "reason": "DAILY_BUDGET_EXHAUSTED"})
                await _record_sync_event(
                    conn,
                    "WARN",
                    "API_FOOTBALL_DAILY_BUDGET_EXHAUSTED",
                    f"API_FOOTBALL daily budget exhausted for {entry.slug}",
                    {"league": entry.slug, "season_year": season_year},
                )
            else:
                try:
                    data = await api_football_client.teams(league=int(league_api_football_id), season=int(season_year))
                    raw_items = (data.get("response") or [])
                    await _record_api_call(
                        conn,
                        source="API_FOOTBALL",
                        endpoint="teams",
                        request_hash=f"teams:league={league_api_football_id}:season={season_year}",
                        request_payload={"league": int(league_api_football_id), "season": int(season_year)},
                        response_status=200,
                        response_hash=str(data.get("results") or len(raw_items)),
                        payload={"league": entry.slug, "results": len(raw_items)},
                    )
                    teams_rows = [
                        {
                            "source": "API_FOOTBALL",
                            "external_id": str((item.get("team") or {}).get("id") or ""),
                            "name": (item.get("team") or {}).get("name"),
                            "country": (item.get("team") or {}).get("country"),
                            "national": bool((item.get("team") or {}).get("national", False)),
                            "venue": item.get("venue") or {},
                            "team": item.get("team") or {},
                        }
                        for item in raw_items
                        if (item.get("team") or {}).get("id")
                    ]
                    if teams_rows:
                        selected_source = "API_FOOTBALL"
                        source_attempts.append({"source": "API_FOOTBALL", "reason": "SELECTED"})
                    else:
                        source_attempts.append({"source": "API_FOOTBALL", "reason": "EMPTY_RESPONSE"})
                except Exception as exc:
                    source_errors.append(f"API_FOOTBALL:{exc}")
                    source_attempts.append({"source": "API_FOOTBALL", "reason": f"ERROR:{exc}"})

        if not teams_rows and league_football_data_code and football_data_client:
            try:
                data = await football_data_client.competition_teams(code=str(league_football_data_code), season=int(season_year))
                raw_items = data.get("teams") or []
                await _record_api_call(
                    conn,
                    source="FOOTBALL_DATA",
                    endpoint="competition_teams",
                    request_hash=f"teams:code={league_football_data_code}:season={season_year}",
                    request_payload={"code": str(league_football_data_code), "season": int(season_year)},
                    response_status=200,
                    response_hash=str(len(raw_items)),
                    payload={"league": entry.slug, "results": len(raw_items)},
                )
                teams_rows = [
                    {
                        "source": "FOOTBALL_DATA",
                        "external_id": str(item.get("id") or ""),
                        "name": item.get("name") or item.get("shortName") or item.get("tla"),
                        "country": ((item.get("area") or {}).get("name") or (item.get("area") or {}).get("code")),
                        "national": False,
                        "venue": {"name": item.get("venue")},
                        "team": item,
                    }
                    for item in raw_items
                    if item.get("id") and (item.get("name") or item.get("shortName") or item.get("tla"))
                ]
                if teams_rows:
                    selected_source = "FOOTBALL_DATA"
                    source_attempts.append({"source": "FOOTBALL_DATA", "reason": "SELECTED"})
                else:
                    source_attempts.append({"source": "FOOTBALL_DATA", "reason": "EMPTY_RESPONSE"})
            except Exception as exc:
                source_errors.append(f"FOOTBALL_DATA:{exc}")
                source_attempts.append({"source": "FOOTBALL_DATA", "reason": f"ERROR:{exc}"})

        if not teams_rows and sportmonks_client and has_sportmonks_capability:
            try:
                data = await sportmonks_client.fixtures(include="participants", page=1, per_page=100)
                fixtures = data.get("data") or []
                parsed_teams: dict[str, dict[str, Any]] = {}
                for fixture in fixtures:
                    for participant in _sportmonks_items(fixture, "participants"):
                        external_id = str(participant.get("id") or "")
                        team_name = participant.get("name")
                        if not external_id or not team_name:
                            continue
                        parsed_teams.setdefault(
                            external_id,
                            {
                                "source": "SPORTMONKS",
                                "external_id": external_id,
                                "name": team_name,
                                "country": None,
                                "national": False,
                                "venue": {"name": participant.get("venue_name")},
                                "team": participant,
                            },
                        )

                teams_rows = list(parsed_teams.values())
                await _record_api_call(
                    conn,
                    source="SPORTMONKS",
                    endpoint="fixtures",
                    request_hash=f"teams:entry={entry.slug}:fixtures:participants",
                    request_payload={"entry": entry.slug, "include": "participants", "page": 1, "per_page": 100},
                    response_status=200,
                    response_hash=str(len(teams_rows)),
                    payload={"league": entry.slug, "fixtures": len(fixtures), "results": len(teams_rows)},
                )
                if teams_rows:
                    selected_source = "SPORTMONKS"
                    source_attempts.append({"source": "SPORTMONKS", "reason": "SELECTED"})
                else:
                    source_attempts.append({"source": "SPORTMONKS", "reason": "EMPTY_RESPONSE"})
            except Exception as exc:
                source_errors.append(f"SPORTMONKS:{exc}")
                source_attempts.append({"source": "SPORTMONKS", "reason": f"ERROR:{exc}"})

        if not teams_rows:
            msg = "; ".join(source_errors) if source_errors else "NO_SOURCE_WITH_DATA"
            errors.append(f"{entry.slug}: {msg}")
            league_stats.append(
                {
                    "league": entry.slug,
                    "status": "ERROR",
                    "teams_processed": 0,
                    "error": msg,
                    "source_attempts": source_attempts,
                }
            )
            await _record_sync_event(
                conn,
                "ERROR",
                "TEAM_SYNC_LEAGUE_ERROR",
                f"team sync failed for {entry.slug}",
                {
                    "league": entry.slug,
                    "api_football_id": str(league_api_football_id or ""),
                    "football_data_code": str(league_football_data_code or ""),
                    "season_year": season_year,
                    "error": msg,
                    "source_attempts": source_attempts,
                },
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

        for item in teams_rows:
            team_info = item.get("team") or {}
            venue_info = item.get("venue") or {}
            external_id = str(item.get("external_id") or "")
            if not external_id:
                continue

            try:
                team_name = str(item.get("name") or team_info.get("name") or f"team-{external_id}")
                team_type = "NATIONAL_TEAM" if bool(item.get("national", False)) else "CLUB"
                country_code = await _resolve_country_code(conn, item.get("country") or team_info.get("country"), country_cache)
                team_id = await _resolve_or_create_team(
                    conn,
                    source=selected_source or "API_FOOTBALL",
                    source_team_id=external_id,
                    display_name=team_name,
                    team_type=team_type,
                    country_code=country_code,
                    metadata={
                        "api_football_id": external_id if (selected_source == "API_FOOTBALL") else None,
                        "football_data_id": external_id if (selected_source == "FOOTBALL_DATA") else None,
                        "sportmonks_id": external_id if (selected_source == "SPORTMONKS") else None,
                        "country": item.get("country") or team_info.get("country"),
                        "founded_year": team_info.get("founded"),
                        "stadium_name": venue_info.get("name"),
                        "stadium_capacity": venue_info.get("capacity"),
                        "logo_url": team_info.get("logo") or team_info.get("crest"),
                        "ingestion_source": selected_source,
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
                        "metadata": _json({"source": selected_source, "external_id": external_id}),
                    },
                )
                total_teams += 1
                league_processed += 1
            except Exception as exc:
                log.warning("team_sync: upsert failed slug=%s team=%s err=%s", entry.slug, item.get("name") or team_info.get("name"), exc)
                errors.append(f"{entry.slug}/{item.get('name') or team_info.get('name')}: {exc}")

        league_status = "OK" if league_processed > 0 else "WARN"
        league_stats.append(
            {
                "league": entry.slug,
                "status": league_status,
                "source": selected_source,
                "teams_processed": league_processed,
                "teams_received": len(teams_rows),
                "source_attempts": source_attempts,
            }
        )
        await _record_sync_event(
            conn,
            "INFO" if league_status == "OK" else "WARN",
            "TEAM_SYNC_LEAGUE_RESULT",
            f"team sync completed for {entry.slug}",
            {
                "league": entry.slug,
                "source": selected_source,
                "teams_processed": league_processed,
                "teams_received": len(teams_rows),
                "status": league_status,
                "source_attempts": source_attempts,
            },
        )

    status = "WARN" if errors else "OK"
    summary = {
        "eligible_leagues": len(
            [
                c
                for c in supported_competitions()
                if (
                    c.source.external_ids.get("API_FOOTBALL")
                    or c.source.external_ids.get("FOOTBALL_DATA")
                    or "teams" in (c.source.capabilities.get("SPORTMONKS") or [])
                )
            ]
        ),
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


async def sync_players_for_all_leagues(conn: AsyncConnection, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upsert players and competition rosters for every catalog entry using source priority.

    Players API is paginated. We iterate pages until paging.current == paging.total.
    This is rate-limit aware: if no API key is configured the loop is skipped gracefully.
    """
    settings = get_settings()
    payload = payload or {}
    max_runtime_seconds = int(payload.get("max_runtime_seconds", 210) or 210)
    ingest_referees = bool(payload.get("ingest_referees", True))
    ingest_venues = bool(payload.get("ingest_venues", True))
    only_match_entities = bool(payload.get("only_match_entities", False))
    max_api_football_last_fixtures = int(payload.get("max_api_football_last_fixtures", 20) or 20)
    max_sportmonks_fixture_pages = int(payload.get("max_sportmonks_fixture_pages", 1) or 1)
    started_at = perf_counter()

    def _time_budget_exceeded() -> bool:
        return (perf_counter() - started_at) >= max_runtime_seconds
    if not settings.api_football_key and not settings.football_data_token and not settings.sportmonks_api_token:
        return {
            "status": "WARN",
            "job_name": "sync_all_leagues_players",
            "records_processed": 0,
            "errors": ["At least one player source credential is required (API_FOOTBALL_KEY, FOOTBALL_DATA_TOKEN or SPORTMONKS_API_TOKEN)."],
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

    async def _resolve_team_id_for_source(team_external_id: str, source: str) -> str | None:
        ref_row = await conn.execute(
            text(
                """
                SELECT entity_id::text
                FROM entity_external_refs
                WHERE entity_type = 'TEAM'
                  AND source = :source
                  AND source_entity_id = :team_external_id
                LIMIT 1
                """
            ),
            {"source": source, "team_external_id": team_external_id},
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
                                    AND coalesce(cte.metadata->>'source', '') = :source
                LIMIT 1
                """
            ),
                        {"cs_id": competition_season_id, "team_external_id": team_external_id, "source": source},
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

    async def _resolve_match_id_for_source(source: str, source_match_id: str) -> str | None:
        if not source_match_id:
            return None
        row = await conn.execute(
            text(
                """
                SELECT entity_id::text
                FROM entity_external_refs
                WHERE entity_type = 'MATCH'
                  AND source = :source
                  AND source_entity_id = :source_match_id
                LIMIT 1
                """
            ),
            {"source": source, "source_match_id": source_match_id},
        )
        return row.scalar_one_or_none()

    async def _upsert_venue_external_ref(venue_id: str, source: str, source_venue_id: str, source_venue_name: str) -> None:
        await conn.execute(
            text(
                """
                INSERT INTO entity_external_refs
                  (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name, confidence, is_primary, payload)
                VALUES
                  ('VENUE', cast(:venue_id as uuid), :source, 'venue', :source_venue_id, :source_venue_name, 1, true,
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
                "venue_id": venue_id,
                "source": source,
                "source_venue_id": source_venue_id,
                "source_venue_name": source_venue_name,
                "payload": _json({"resolver": "match_entities_sync", "source": source}),
            },
        )

    async def _resolve_or_create_venue(
        *,
        source: str,
        source_venue_id: str,
        display_name: str,
        city: str | None,
        country_code: str | None,
        timezone_name: str | None,
        latitude: float | None,
        longitude: float | None,
        metadata: dict[str, Any],
    ) -> str:
        venue_id: str | None = None
        if source_venue_id:
            row = await conn.execute(
                text(
                    """
                    SELECT entity_id::text
                    FROM entity_external_refs
                    WHERE entity_type = 'VENUE'
                      AND source = :source
                      AND source_entity_id = :source_venue_id
                    LIMIT 1
                    """
                ),
                {"source": source, "source_venue_id": source_venue_id},
            )
            venue_id = row.scalar_one_or_none()

        if not venue_id:
            row = await conn.execute(
                text(
                    """
                    SELECT venue_id::text
                    FROM venues
                    WHERE lower(display_name) = lower(:display_name)
                      AND coalesce(lower(city), '') = coalesce(lower(:city), '')
                      AND coalesce(country_code, '') = coalesce(:country_code, '')
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "display_name": display_name,
                    "city": city,
                    "country_code": country_code,
                },
            )
            venue_id = row.scalar_one_or_none()

        if not venue_id:
            slug = f"{_slug(display_name)}-{(city or 'na').lower()}"[:180]
            created = await conn.execute(
                text(
                    """
                    INSERT INTO venues
                      (slug, display_name, city, country_code, timezone_name, latitude, longitude, metadata)
                    VALUES
                      (:slug, :display_name, :city, :country_code, :timezone_name, :latitude, :longitude, cast(:metadata as jsonb))
                    RETURNING venue_id::text
                    """
                ),
                {
                    "slug": slug,
                    "display_name": display_name,
                    "city": city,
                    "country_code": country_code,
                    "timezone_name": timezone_name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "metadata": _json(metadata),
                },
            )
            venue_id = created.scalar_one()
        else:
            await conn.execute(
                text(
                    """
                    UPDATE venues
                    SET display_name = COALESCE(:display_name, display_name),
                        city = COALESCE(:city, city),
                        country_code = COALESCE(:country_code, country_code),
                        timezone_name = COALESCE(:timezone_name, timezone_name),
                        latitude = COALESCE(:latitude, latitude),
                        longitude = COALESCE(:longitude, longitude),
                        metadata = venues.metadata || cast(:metadata as jsonb),
                        updated_at = now()
                    WHERE venue_id = cast(:venue_id as uuid)
                    """
                ),
                {
                    "venue_id": venue_id,
                    "display_name": display_name,
                    "city": city,
                    "country_code": country_code,
                    "timezone_name": timezone_name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "metadata": _json(metadata),
                },
            )

        if source_venue_id:
            await _upsert_venue_external_ref(venue_id, source, source_venue_id, display_name)
        return venue_id

    async def _upsert_referee_external_ref(referee_id: str, source: str, source_referee_id: str, source_referee_name: str) -> None:
        await conn.execute(
            text(
                """
                INSERT INTO entity_external_refs
                  (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name, confidence, is_primary, payload)
                VALUES
                  ('REFEREE', cast(:referee_id as uuid), :source, 'referee', :source_referee_id, :source_referee_name, 1, true,
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
                "referee_id": referee_id,
                "source": source,
                "source_referee_id": source_referee_id,
                "source_referee_name": source_referee_name,
                "payload": _json({"resolver": "match_entities_sync", "source": source}),
            },
        )

    async def _resolve_or_create_referee(
        *,
        source: str,
        source_referee_id: str,
        display_name: str,
        nationality_country_code: str | None,
        metadata: dict[str, Any],
    ) -> str:
        referee_id: str | None = None
        if source_referee_id:
            row = await conn.execute(
                text(
                    """
                    SELECT entity_id::text
                    FROM entity_external_refs
                    WHERE entity_type = 'REFEREE'
                      AND source = :source
                      AND source_entity_id = :source_referee_id
                    LIMIT 1
                    """
                ),
                {"source": source, "source_referee_id": source_referee_id},
            )
            referee_id = row.scalar_one_or_none()

        if not referee_id:
            row = await conn.execute(
                text(
                    """
                    SELECT referee_id::text
                    FROM referees
                    WHERE normalized_name = :normalized_name
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
                    "nationality_country_code": nationality_country_code,
                },
            )
            referee_id = row.scalar_one_or_none()

        if not referee_id:
            slug = f"{_slug(display_name)}-{source.lower()}-{source_referee_id or _slug(display_name)}"[:180]
            created = await conn.execute(
                text(
                    """
                    INSERT INTO referees
                      (slug, display_name, normalized_name, nationality_country_code, metadata)
                    VALUES
                      (:slug, :display_name, :normalized_name, :nationality_country_code, cast(:metadata as jsonb))
                    RETURNING referee_id::text
                    """
                ),
                {
                    "slug": slug,
                    "display_name": display_name,
                    "normalized_name": _norm(display_name),
                    "nationality_country_code": nationality_country_code,
                    "metadata": _json(metadata),
                },
            )
            referee_id = created.scalar_one()
        else:
            await conn.execute(
                text(
                    """
                    UPDATE referees
                    SET display_name = COALESCE(:display_name, display_name),
                        normalized_name = COALESCE(:normalized_name, normalized_name),
                        nationality_country_code = COALESCE(:nationality_country_code, nationality_country_code),
                        metadata = referees.metadata || cast(:metadata as jsonb),
                        updated_at = now()
                    WHERE referee_id = cast(:referee_id as uuid)
                    """
                ),
                {
                    "referee_id": referee_id,
                    "display_name": display_name,
                    "normalized_name": _norm(display_name),
                    "nationality_country_code": nationality_country_code,
                    "metadata": _json(metadata),
                },
            )

        if source_referee_id:
            await _upsert_referee_external_ref(referee_id, source, source_referee_id, display_name)
        return referee_id

    async def _upsert_match_official(match_id: str, referee_id: str, role: str, metadata: dict[str, Any]) -> None:
        await conn.execute(
            text(
                """
                INSERT INTO match_officials
                  (match_id, referee_id, role, metadata)
                VALUES
                  (cast(:match_id as uuid), cast(:referee_id as uuid), cast(:role as official_role), cast(:metadata as jsonb))
                ON CONFLICT (match_id, referee_id, role) DO UPDATE SET
                  metadata = match_officials.metadata || excluded.metadata
                """
            ),
            {
                "match_id": match_id,
                "referee_id": referee_id,
                "role": role,
                "metadata": _json(metadata),
            },
        )

    def _normalize_official_role(value: str | None) -> str:
        role = str(value or "").strip().upper().replace(" ", "_")
        mapping = {
            "REFEREE": "REFEREE",
            "MAIN": "REFEREE",
            "ASSISTANT": "ASSISTANT_REFEREE",
            "ASSISTANT_REFEREE": "ASSISTANT_REFEREE",
            "VAR": "VAR",
            "FOURTH_OFFICIAL": "FOURTH_OFFICIAL",
            "FOURTH": "FOURTH_OFFICIAL",
        }
        return mapping.get(role, "OTHER")

    async def _ingest_match_entities_for_league(entry_slug: str, league_id: str | None, season_year: str, selected_source: str | None) -> dict[str, int]:
        stats = {"venues_upserted": 0, "referees_upserted": 0, "official_links": 0}
        if not ingest_referees and not ingest_venues:
            return stats
        if _time_budget_exceeded():
            return stats

        if selected_source == "SPORTMONKS" and sportmonks_client:
            try:
                pages = max(1, max_sportmonks_fixture_pages)
                for page in range(1, pages + 1):
                    if _time_budget_exceeded():
                        break
                    data = await sportmonks_client.fixtures(
                        include="venue;referees",
                        page=page,
                        per_page=50,
                    )
                    fixtures = data.get("data") or []
                    if not fixtures:
                        break
                    for fixture in fixtures:
                        if _time_budget_exceeded():
                            break
                        source_match_id = str(fixture.get("id") or "")
                        match_id = await _resolve_match_id_for_source("SPORTMONKS", source_match_id)
                        venue = fixture.get("venue") if isinstance(fixture.get("venue"), dict) else {}
                        if ingest_venues and venue:
                            venue_id = await _resolve_or_create_venue(
                                source="SPORTMONKS",
                                source_venue_id=str(venue.get("id") or ""),
                                display_name=str(venue.get("name") or "Unknown Venue"),
                                city=venue.get("city_name") or venue.get("city"),
                                country_code=await _resolve_country_code(conn, venue.get("country_name") or venue.get("country"), country_cache),
                                timezone_name=venue.get("timezone"),
                                latitude=venue.get("latitude"),
                                longitude=venue.get("longitude"),
                                metadata={"source": "SPORTMONKS", "league": entry_slug},
                            )
                            if venue_id:
                                stats["venues_upserted"] += 1
                                if match_id:
                                    await conn.execute(
                                        text(
                                            """
                                            UPDATE matches
                                            SET venue_id = cast(:venue_id as uuid),
                                                updated_at = now()
                                            WHERE match_id = cast(:match_id as uuid)
                                            """
                                        ),
                                        {"match_id": match_id, "venue_id": venue_id},
                                    )

                        if ingest_referees:
                            for ref in _sportmonks_items(fixture, "referees"):
                                display_name = str(ref.get("name") or "").strip()
                                if not display_name:
                                    continue
                                referee_id = await _resolve_or_create_referee(
                                    source="SPORTMONKS",
                                    source_referee_id=str(ref.get("id") or ""),
                                    display_name=display_name,
                                    nationality_country_code=await _resolve_country_code(conn, ref.get("country") or ref.get("nationality"), country_cache),
                                    metadata={"source": "SPORTMONKS", "league": entry_slug},
                                )
                                stats["referees_upserted"] += 1
                                if match_id:
                                    await _upsert_match_official(
                                        match_id,
                                        referee_id,
                                        _normalize_official_role(ref.get("type") or ref.get("role")),
                                        {"source": "SPORTMONKS", "league": entry_slug},
                                    )
                                    stats["official_links"] += 1
            except Exception as exc:
                errors.append(f"{entry_slug} sportmonks entities: {exc}")
            return stats

        if league_id and api_football_client:
            try:
                if not await _api_football_has_budget(conn, settings):
                    return stats
                data = await api_football_client.fixtures(
                    league=int(league_id),
                    season=int(season_year),
                    last_count=max(1, max_api_football_last_fixtures),
                )
                fixtures = data.get("response") or []
                await _record_api_call(
                    conn,
                    source="API_FOOTBALL",
                    endpoint="fixtures",
                    request_hash=f"entities:league={league_id}:season={season_year}:last={max_api_football_last_fixtures}",
                    request_payload={"league": int(league_id), "season": int(season_year), "last": int(max_api_football_last_fixtures)},
                    response_status=200,
                    response_hash=str(len(fixtures)),
                    payload={"league": entry_slug, "fixtures": len(fixtures)},
                )
                for fixture in fixtures:
                    if _time_budget_exceeded():
                        break
                    fixture_node = fixture.get("fixture") or {}
                    source_match_id = str(fixture_node.get("id") or "")
                    match_id = await _resolve_match_id_for_source("API_FOOTBALL", source_match_id)

                    venue = fixture_node.get("venue") or {}
                    if ingest_venues and venue:
                        venue_id = await _resolve_or_create_venue(
                            source="API_FOOTBALL",
                            source_venue_id=str(venue.get("id") or ""),
                            display_name=str(venue.get("name") or "Unknown Venue"),
                            city=venue.get("city"),
                            country_code=None,
                            timezone_name=fixture_node.get("timezone"),
                            latitude=None,
                            longitude=None,
                            metadata={"source": "API_FOOTBALL", "league": entry_slug},
                        )
                        if venue_id:
                            stats["venues_upserted"] += 1
                            if match_id:
                                await conn.execute(
                                    text(
                                        """
                                        UPDATE matches
                                        SET venue_id = cast(:venue_id as uuid),
                                            updated_at = now()
                                        WHERE match_id = cast(:match_id as uuid)
                                        """
                                    ),
                                    {"match_id": match_id, "venue_id": venue_id},
                                )

                    if ingest_referees:
                        referee_name = str(fixture_node.get("referee") or "").strip()
                        if referee_name:
                            referee_id = await _resolve_or_create_referee(
                                source="API_FOOTBALL",
                                source_referee_id=source_match_id,
                                display_name=referee_name,
                                nationality_country_code=None,
                                metadata={"source": "API_FOOTBALL", "league": entry_slug},
                            )
                            stats["referees_upserted"] += 1
                            if match_id:
                                await _upsert_match_official(
                                    match_id,
                                    referee_id,
                                    "REFEREE",
                                    {"source": "API_FOOTBALL", "league": entry_slug},
                                )
                                stats["official_links"] += 1
            except Exception as exc:
                errors.append(f"{entry_slug} api-football entities: {exc}")
        return stats

    async def _reconcile_removed_rosters(active_pairs: list[dict[str, str]], source: str) -> int:
        if not active_pairs:
            result = await conn.execute(
                text(
                    """
                    UPDATE competition_rosters cr
                    SET roster_status = 'CUT',
                        updated_at = now(),
                        metadata = cr.metadata || cast(:metadata as jsonb)
                    WHERE cr.competition_season_id = cast(:cs_id as uuid)
                      AND coalesce(cr.metadata->>'source','') = :source
                      AND cr.roster_status in ('ACTIVE', 'CALLED_UP', 'UNKNOWN')
                    """
                ),
                {
                    "cs_id": competition_season_id,
                    "source": source,
                    "metadata": _json({"deactivated_by": "player_sync", "source": source}),
                },
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
                                        metadata = cr.metadata || cast(:metadata as jsonb)
                WHERE cr.competition_season_id = cast(:cs_id as uuid)
                                    AND coalesce(cr.metadata->>'source','') = :source
                  AND cr.roster_status in ('ACTIVE', 'CALLED_UP', 'UNKNOWN')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM active_pairs a
                    WHERE a.team_id = cr.team_id
                      AND a.player_id = cr.player_id
                  )
                """
            ),
            {
                "cs_id": competition_season_id,
                "source": source,
                "active_pairs": _json(active_pairs),
                "metadata": _json({"deactivated_by": "player_sync", "source": source}),
            },
        )
        return int(result.rowcount or 0)

    api_football_client = ApiFootballClient() if settings.api_football_key else None
    football_data_client = FootballDataClient() if settings.football_data_token else None
    sportmonks_client = SportmonksClient() if settings.sportmonks_api_token else None
    total_players = 0
    total_entities = 0
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
        # Ensure the season exists for every catalog entry before syncing source data.
        await seed_competition_catalog(conn, entry.slug)
        if _time_budget_exceeded():
            skipped.append({"league": entry.slug, "reason": "TIME_BUDGET_EXCEEDED"})
            continue
        league_id = entry.source.external_ids.get("API_FOOTBALL")
        football_data_code = entry.source.external_ids.get("FOOTBALL_DATA")
        has_sportmonks_players_capability = "players" in (entry.source.capabilities.get("SPORTMONKS") or [])
        if not league_id and not football_data_code and not has_sportmonks_players_capability:
            skipped.append({"league": entry.slug, "reason": "MISSING_SUPPORTED_EXTERNAL_ID"})
            continue
        if (
            "players" not in (entry.source.capabilities.get("API_FOOTBALL") or [])
            and "players" not in (entry.source.capabilities.get("FOOTBALL_DATA") or [])
            and "players" not in (entry.source.capabilities.get("SPORTMONKS") or [])
        ):
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
        entity_stats = {"venues_upserted": 0, "referees_upserted": 0, "official_links": 0}
        active_pairs: list[dict[str, str]] = []
        selected_source: str | None = None
        source_attempts: list[dict[str, str]] = []

        if only_match_entities:
            if league_id and api_football_client and "venues" in (entry.source.capabilities.get("API_FOOTBALL") or []):
                selected_source = "API_FOOTBALL"
                source_attempts.append({"source": "API_FOOTBALL", "reason": "SELECTED_ENTITIES"})
            elif sportmonks_client and "venues" in (entry.source.capabilities.get("SPORTMONKS") or []):
                selected_source = "SPORTMONKS"
                source_attempts.append({"source": "SPORTMONKS", "reason": "SELECTED_ENTITIES"})
            else:
                source_attempts.append({"source": "ALL", "reason": "NO_SOURCE_WITH_ENTITIES_DATA"})

            if selected_source is not None and not _time_budget_exceeded() and (ingest_referees or ingest_venues):
                entity_stats = await _ingest_match_entities_for_league(
                    entry_slug=entry.slug,
                    league_id=str(league_id) if league_id else None,
                    season_year=season_year,
                    selected_source=selected_source,
                )
                total_entities += int(entity_stats["venues_upserted"]) + int(entity_stats["referees_upserted"]) + int(entity_stats["official_links"])
                league_status = "OK" if (entity_stats["venues_upserted"] + entity_stats["referees_upserted"] + entity_stats["official_links"]) > 0 else "WARN"
            else:
                league_status = "WARN"

            league_stats.append(
                {
                    "league": entry.slug,
                    "status": league_status,
                    "source": selected_source,
                    "players_processed": 0,
                    "pages_processed": 0,
                    "errors": league_errors,
                    "rosters_deactivated": 0,
                    "venues_upserted": entity_stats["venues_upserted"],
                    "referees_upserted": entity_stats["referees_upserted"],
                    "official_links": entity_stats["official_links"],
                    "source_attempts": source_attempts,
                }
            )
            await _record_sync_event(
                conn,
                "INFO" if league_status == "OK" else "WARN",
                "MATCH_ENTITIES_SYNC_LEAGUE_RESULT",
                f"match entities sync completed for {entry.slug}",
                {
                    "league": entry.slug,
                    "source": selected_source,
                    "venues_upserted": entity_stats["venues_upserted"],
                    "referees_upserted": entity_stats["referees_upserted"],
                    "official_links": entity_stats["official_links"],
                    "status": league_status,
                    "source_attempts": source_attempts,
                },
            )
            continue

        if league_id and api_football_client and "players" in (entry.source.capabilities.get("API_FOOTBALL") or []):
            page = 1
            while True:
                if not await _api_football_has_budget(conn, settings):
                    skipped.append({"league": entry.slug, "reason": "API_FOOTBALL_DAILY_BUDGET_EXHAUSTED"})
                    source_attempts.append({"source": "API_FOOTBALL", "reason": "DAILY_BUDGET_EXHAUSTED"})
                    await _record_sync_event(
                        conn,
                        "WARN",
                        "API_FOOTBALL_DAILY_BUDGET_EXHAUSTED",
                        f"API_FOOTBALL daily budget exhausted for {entry.slug}",
                        {"league": entry.slug, "season_year": season_year},
                    )
                    break

                try:
                    data = await api_football_client.players(league=int(league_id), season=int(season_year), page=page)
                    await _record_api_call(
                        conn,
                        source="API_FOOTBALL",
                        endpoint="players",
                        request_hash=f"players:league={league_id}:season={season_year}:page={page}",
                        request_payload={"league": int(league_id), "season": int(season_year), "page": page},
                        response_status=200,
                        response_hash=str((data.get("paging") or {}).get("current") or page),
                        payload={"league": entry.slug, "page": page, "results": len(data.get("response") or [])},
                    )
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
                selected_source = "API_FOOTBALL"
                if pages_processed == 1:
                    source_attempts.append({"source": "API_FOOTBALL", "reason": "SELECTED"})

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

                        if team_info.get("id"):
                            team_ext = str(team_info["id"])
                            team_id = await _resolve_team_id_for_source(team_ext, "API_FOOTBALL")
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

        if selected_source is None and football_data_code and football_data_client:
            try:
                data = await football_data_client.competition_teams(code=str(football_data_code), season=int(season_year))
                teams = data.get("teams") or []
                await _record_api_call(
                    conn,
                    source="FOOTBALL_DATA",
                    endpoint="competition_teams",
                    request_hash=f"players:code={football_data_code}:season={season_year}",
                    request_payload={"code": str(football_data_code), "season": int(season_year)},
                    response_status=200,
                    response_hash=str(len(teams)),
                    payload={"league": entry.slug, "teams": len(teams)},
                )
                candidate_active_pairs: list[dict[str, str]] = []
                football_data_players_processed = 0
                for team in teams:
                    team_ext = str(team.get("id") or "")
                    if not team_ext:
                        continue
                    squad = team.get("squad") or []
                    for player_info in squad:
                        external_id = str(player_info.get("id") or "")
                        if not external_id:
                            continue
                        try:
                            player_name = player_info.get("name") or f"player-{external_id}"
                            player_id = await _resolve_or_create_player(
                                source="FOOTBALL_DATA",
                                source_player_id=external_id,
                                display_name=player_name,
                                birth_date=player_info.get("dateOfBirth"),
                                nationality_country_code=await _resolve_country_code(conn, player_info.get("nationality"), country_cache),
                                metadata={
                                    "football_data_id": external_id,
                                    "position": player_info.get("position"),
                                    "shirt_number": player_info.get("shirtNumber"),
                                    "nationality": player_info.get("nationality"),
                                    "ingestion_source": "FOOTBALL_DATA",
                                },
                            )
                            team_id = await _resolve_team_id_for_source(team_ext, "FOOTBALL_DATA")
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
                                        "number": player_info.get("shirtNumber"),
                                        "metadata": _json(
                                            {
                                                "source": "FOOTBALL_DATA",
                                                "team_external_id": team_ext,
                                                "league": entry.slug,
                                            }
                                        ),
                                    },
                                )
                                await _upsert_team_membership(player_id, team_id, "FOOTBALL_DATA")
                                candidate_active_pairs.append({"team_id": team_id, "player_id": player_id})
                            total_players += 1
                            league_processed += 1
                            football_data_players_processed += 1
                        except Exception as exc:
                            log.warning("player_sync: football-data upsert failed slug=%s player=%s err=%s", entry.slug, player_info.get("name"), exc)
                            errors.append(f"{entry.slug}/{player_info.get('name')}: {exc}")
                            league_errors += 1

                if football_data_players_processed > 0:
                    selected_source = "FOOTBALL_DATA"
                    pages_processed = 1
                    active_pairs.extend(candidate_active_pairs)
                    source_attempts.append({"source": "FOOTBALL_DATA", "reason": "SELECTED"})
                else:
                    source_attempts.append({"source": "FOOTBALL_DATA", "reason": "EMPTY_RESPONSE"})
            except Exception as exc:
                errors.append(f"{entry.slug} football-data: {exc}")
                league_errors += 1
                source_attempts.append({"source": "FOOTBALL_DATA", "reason": f"ERROR:{exc}"})

        if selected_source is None and sportmonks_client and has_sportmonks_players_capability:
            try:
                data = await sportmonks_client.fixtures(include="participants;lineups;lineups.player", page=1, per_page=100)
                fixtures = data.get("data") or []
                candidate_active_pairs: list[dict[str, str]] = []
                sportmonks_players_processed = 0

                for fixture in fixtures:
                    participants = {
                        str(p.get("id")): p
                        for p in _sportmonks_items(fixture, "participants")
                        if p.get("id")
                    }

                    for lineup in _sportmonks_items(fixture, "lineups"):
                        player_node = lineup.get("player") if isinstance(lineup.get("player"), dict) else {}
                        external_id = str(player_node.get("id") or lineup.get("player_id") or "")
                        if not external_id:
                            continue

                        team_ext = str(lineup.get("participant_id") or lineup.get("team_id") or "")
                        participant = participants.get(team_ext, {})
                        player_name = player_node.get("name") or lineup.get("player_name") or f"player-{external_id}"

                        try:
                            player_id = await _resolve_or_create_player(
                                source="SPORTMONKS",
                                source_player_id=external_id,
                                display_name=player_name,
                                birth_date=player_node.get("date_of_birth"),
                                nationality_country_code=await _resolve_country_code(conn, player_node.get("nationality"), country_cache),
                                metadata={
                                    "sportmonks_id": external_id,
                                    "position": player_node.get("position") or lineup.get("position"),
                                    "shirt_number": player_node.get("number") or lineup.get("jersey_number"),
                                    "nationality": player_node.get("nationality"),
                                    "ingestion_source": "SPORTMONKS",
                                },
                            )

                            if team_ext:
                                team_id = await _resolve_team_id_for_source(team_ext, "SPORTMONKS")
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
                                            "position": player_node.get("position") or lineup.get("position"),
                                            "number": player_node.get("number") or lineup.get("jersey_number"),
                                            "metadata": _json(
                                                {
                                                    "source": "SPORTMONKS",
                                                    "team_external_id": team_ext,
                                                    "team_name": participant.get("name"),
                                                    "league": entry.slug,
                                                }
                                            ),
                                        },
                                    )
                                    await _upsert_team_membership(player_id, team_id, "SPORTMONKS")
                                    candidate_active_pairs.append({"team_id": team_id, "player_id": player_id})

                            total_players += 1
                            league_processed += 1
                            sportmonks_players_processed += 1
                        except Exception as exc:
                            log.warning("player_sync: sportmonks upsert failed slug=%s player=%s err=%s", entry.slug, player_name, exc)
                            errors.append(f"{entry.slug}/{player_name}: {exc}")
                            league_errors += 1

                await _record_api_call(
                    conn,
                    source="SPORTMONKS",
                    endpoint="fixtures",
                    request_hash=f"players:entry={entry.slug}:fixtures:lineups",
                    request_payload={"entry": entry.slug, "include": "participants;lineups;lineups.player", "page": 1, "per_page": 100},
                    response_status=200,
                    response_hash=str(len(fixtures)),
                    payload={"league": entry.slug, "fixtures": len(fixtures), "players_processed": league_processed},
                )

                if sportmonks_players_processed > 0:
                    selected_source = "SPORTMONKS"
                    pages_processed = 1
                    active_pairs.extend(candidate_active_pairs)
                    source_attempts.append({"source": "SPORTMONKS", "reason": "SELECTED"})
                else:
                    source_attempts.append({"source": "SPORTMONKS", "reason": "EMPTY_RESPONSE"})
            except Exception as exc:
                errors.append(f"{entry.slug} sportmonks: {exc}")
                league_errors += 1
                source_attempts.append({"source": "SPORTMONKS", "reason": f"ERROR:{exc}"})

        if selected_source is None:
            skipped.append({"league": entry.slug, "reason": "NO_SOURCE_WITH_PLAYER_DATA"})
            source_attempts.append({"source": "ALL", "reason": "NO_SOURCE_WITH_PLAYER_DATA"})

        if selected_source is not None and not _time_budget_exceeded() and (ingest_referees or ingest_venues):
            entity_stats = await _ingest_match_entities_for_league(
                entry_slug=entry.slug,
                league_id=str(league_id) if league_id else None,
                season_year=season_year,
                selected_source=selected_source,
            )
            total_entities += int(entity_stats["venues_upserted"]) + int(entity_stats["referees_upserted"]) + int(entity_stats["official_links"])

        deactivated_rosters = 0
        if selected_source is not None:
            deactivated_rosters = await _reconcile_removed_rosters(active_pairs, selected_source)

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
                "source": selected_source,
                "players_processed": league_processed,
                "pages_processed": pages_processed,
                "errors": league_errors,
                "rosters_deactivated": deactivated_rosters,
                "venues_upserted": entity_stats["venues_upserted"],
                "referees_upserted": entity_stats["referees_upserted"],
                "official_links": entity_stats["official_links"],
                "source_attempts": source_attempts,
            }
        )
        await _record_sync_event(
            conn,
            "INFO" if league_status == "OK" else "WARN",
            "PLAYER_SYNC_LEAGUE_RESULT",
            f"player sync completed for {entry.slug}",
            {
                "league": entry.slug,
                "source": selected_source,
                "players_processed": league_processed,
                "pages_processed": pages_processed,
                "errors": league_errors,
                "rosters_deactivated": deactivated_rosters,
                "venues_upserted": entity_stats["venues_upserted"],
                "referees_upserted": entity_stats["referees_upserted"],
                "official_links": entity_stats["official_links"],
                "status": league_status,
                "source_attempts": source_attempts,
            },
        )

    status = "WARN" if errors else "OK"
    summary = {
        "eligible_leagues": len(
            [
                c
                for c in supported_competitions()
                if (c.source.external_ids.get("API_FOOTBALL") and "players" in (c.source.capabilities.get("API_FOOTBALL") or []))
                or (c.source.external_ids.get("FOOTBALL_DATA") and "players" in (c.source.capabilities.get("FOOTBALL_DATA") or []))
                or ("players" in (c.source.capabilities.get("SPORTMONKS") or []))
            ]
        ),
        "processed_leagues": len([s for s in league_stats if s.get("status") == "OK"]),
        "warn_leagues": len([s for s in league_stats if s.get("status") == "WARN"]),
        "error_leagues": len([s for s in league_stats if s.get("status") == "ERROR"]),
        "skipped_leagues": len(skipped),
        "max_runtime_seconds": max_runtime_seconds,
        "elapsed_seconds": int(perf_counter() - started_at),
    }
    return {
        "status": status,
        "job_name": "sync_all_leagues_players",
        "schema_mode": schema_mode,
        "records_processed": total_players + total_entities,
        "summary": summary,
        "entities_processed": total_entities,
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
        has_team_source = any(
            "teams" in (entry.source.capabilities.get(source) or [])
            for source in [entry.source.primary, *(entry.source.secondary or [])]
        )
        if not has_team_source:
            skipped.append({"league": entry.slug, "reason": "NO_TEAMS_CAPABILITY"})
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

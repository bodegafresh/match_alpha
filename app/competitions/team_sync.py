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
    return _norm(name).replace(" ", "-")


def _country_code_or_none(country: str | None) -> str | None:
    value = (country or "").strip().upper()
    if len(value) == 2 and value.isalpha():
        return value
    return None


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
                team_row = await conn.execute(
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
                        "slug": team_slug,
                        "team_type": "NATIONAL_TEAM" if bool(team_info.get("national", False)) else "CLUB",
                        "display_name": team_info.get("name", team_slug),
                        "normalized_name": _norm(team_info.get("name", team_slug)),
                        "country_code": _country_code_or_none(team_info.get("country")),
                        "metadata": _json(
                            {
                                "api_football_id": external_id,
                                "country": team_info.get("country"),
                                "founded_year": team_info.get("founded"),
                                "stadium_name": venue_info.get("name"),
                                "stadium_capacity": venue_info.get("capacity"),
                                "logo_url": team_info.get("logo"),
                            }
                        ),
                    },
                )
                team_id = team_row.scalar_one()

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

    client = ApiFootballClient()
    total_players = 0
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    league_stats: list[dict[str, Any]] = []
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

                player_slug = _slug(f"{player_info.get('firstname', '')} {player_info.get('lastname', '')}")
                external_id = str(player_info["id"])

                try:
                    player_row = await conn.execute(
                        text(
                            """
                            INSERT INTO players
                              (slug, display_name, normalized_name, birth_date,
                               nationality_country_code, metadata)
                            VALUES
                              (:slug, :display_name, :normalized_name, cast(:dob as date),
                               :nationality_country_code, cast(:metadata as jsonb))
                            ON CONFLICT (slug) DO UPDATE SET
                              display_name = excluded.display_name,
                              normalized_name = excluded.normalized_name,
                              birth_date = COALESCE(excluded.birth_date, players.birth_date),
                              nationality_country_code = COALESCE(
                                excluded.nationality_country_code,
                                players.nationality_country_code
                              ),
                              metadata = players.metadata || excluded.metadata,
                              updated_at = now()
                            RETURNING player_id::text
                            """
                        ),
                        {
                            "slug": player_slug or f"player-{external_id}",
                            "display_name": player_info.get("name")
                            or f"{player_info.get('firstname','')} {player_info.get('lastname','')}".strip(),
                            "normalized_name": _norm(
                                player_info.get("name")
                                or f"{player_info.get('firstname','')} {player_info.get('lastname','')}".strip()
                            ),
                            "dob": player_info.get("birth", {}).get("date"),
                            "nationality_country_code": _country_code_or_none(player_info.get("nationality")),
                            "metadata": _json(
                                {
                                    "api_football_id": external_id,
                                    "position": player_info.get("position"),
                                    "number": player_info.get("number"),
                                    "photo_url": player_info.get("photo"),
                                    "nationality": player_info.get("nationality"),
                                }
                            ),
                        },
                    )
                    player_id = player_row.scalar_one()

                    # Roster link: find team_id by API-Football team id stored in competition_team_entries.metadata.
                    if team_info.get("id"):
                        team_ext = str(team_info["id"])
                        team_row = await conn.execute(
                            text(
                                """
                                SELECT t.team_id::text FROM teams t
                                JOIN competition_team_entries cte ON cte.team_id = t.team_id
                                WHERE cte.competition_season_id = cast(:cs_id as uuid)
                                  AND cte.metadata->>'external_id' = :ext_id
                                LIMIT 1
                                """
                            ),
                            {"cs_id": competition_season_id, "ext_id": team_ext},
                        )
                        team_link = team_row.fetchone()
                        if team_link:
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
                                      metadata = competition_rosters.metadata || excluded.metadata,
                                      updated_at = now()
                                    """
                                ),
                                {
                                    "cs_id": competition_season_id,
                                    "team_id": team_link[0],
                                    "player_id": player_id,
                                    "position": player_info.get("position"),
                                    "number": player_info.get("number"),
                                    "metadata": _json({"source": "API_FOOTBALL", "team_external_id": team_ext}),
                                },
                            )
                    total_players += 1
                    league_processed += 1
                except Exception as exc:
                    log.warning("player_sync: upsert failed slug=%s player=%s err=%s", entry.slug, player_info.get("name"), exc)
                    errors.append(f"{entry.slug}/{player_info.get('name')}: {exc}")
                    league_errors += 1

            if not items or paging.get("current") >= paging.get("total", 1):
                break
            page += 1

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

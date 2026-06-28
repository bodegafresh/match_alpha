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
from app.core.time import iso_utc, utc_now

log = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFD", str(name or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def _slug(name: str) -> str:
    return _norm(name).replace(" ", "-")


async def sync_teams_for_all_leagues(conn: AsyncConnection) -> dict[str, Any]:
    """Upsert teams for every catalog entry with an API_FOOTBALL external_id."""
    client = ApiFootballClient()
    total_teams = 0
    errors: list[str] = []

    for entry in supported_competitions():
        league_id = entry.source.external_ids.get("API_FOOTBALL")
        if not league_id:
            continue

        # Derive season year from the entry's start date (e.g. "2026" from "2026-06-01")
        season_year = (entry.starts_at or "")[:4]
        if not season_year.isdigit():
            continue

        try:
            data = await client.teams(league=int(league_id), season=int(season_year))
            teams_raw = (data.get("response") or [])
        except Exception as exc:
            log.warning("team_sync: API-Football teams failed league=%s season=%s err=%s", league_id, season_year, exc)
            errors.append(f"{entry.slug}: {exc}")
            continue

        # Get or create competition_season_id
        cs_row = await conn.execute(
            text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
            {"slug": entry.slug},
        )
        cs = cs_row.fetchone()
        if not cs:
            log.debug("team_sync: season not seeded yet slug=%s, skipping", entry.slug)
            continue
        competition_season_id = cs[0]

        for item in teams_raw:
            team_info = item.get("team") or {}
            venue_info = item.get("venue") or {}
            if not team_info.get("id"):
                continue

            team_slug = _slug(team_info.get("name", ""))
            external_id = str(team_info["id"])

            try:
                team_row = await conn.execute(
                    text("""
                        INSERT INTO teams
                          (slug, display_name, short_name, country_code, is_national_team,
                           founded_year, stadium_name, stadium_capacity, logo_url, metadata)
                        VALUES
                          (:slug, :display_name, :short_name, :country_code, :is_national,
                           :founded_year, :stadium_name, :stadium_capacity, :logo_url,
                           cast(:metadata as jsonb))
                        ON CONFLICT (slug) DO UPDATE SET
                          display_name = excluded.display_name,
                          logo_url = COALESCE(excluded.logo_url, teams.logo_url),
                          stadium_name = COALESCE(excluded.stadium_name, teams.stadium_name),
                          stadium_capacity = COALESCE(excluded.stadium_capacity, teams.stadium_capacity),
                          metadata = teams.metadata || excluded.metadata,
                          updated_at = now()
                        RETURNING team_id::text
                    """),
                    {
                        "slug": team_slug,
                        "display_name": team_info.get("name", team_slug),
                        "short_name": (team_info.get("code") or "")[:10] or None,
                        "country_code": (team_info.get("country") or "")[:2].upper() or None,
                        "is_national": bool(team_info.get("national", False)),
                        "founded_year": team_info.get("founded"),
                        "stadium_name": venue_info.get("name"),
                        "stadium_capacity": venue_info.get("capacity"),
                        "logo_url": team_info.get("logo"),
                        "metadata": _json({"api_football_id": external_id, "country": team_info.get("country")}),
                    },
                )
                team_id = team_row.scalar_one()

                # Link team to competition_season
                await conn.execute(
                    text("""
                        INSERT INTO competition_season_teams
                          (competition_season_id, team_id, external_id, source)
                        VALUES
                          (cast(:cs_id as uuid), cast(:team_id as uuid), :external_id, 'API_FOOTBALL')
                        ON CONFLICT (competition_season_id, team_id) DO UPDATE SET
                          external_id = excluded.external_id,
                          updated_at = now()
                    """),
                    {"cs_id": competition_season_id, "team_id": team_id, "external_id": external_id},
                )
                total_teams += 1
            except Exception as exc:
                log.warning("team_sync: upsert failed slug=%s team=%s err=%s", entry.slug, team_info.get("name"), exc)
                errors.append(f"{entry.slug}/{team_info.get('name')}: {exc}")

    status = "WARN" if errors else "OK"
    return {
        "status": status,
        "job_name": "sync_all_leagues_teams",
        "records_processed": total_teams,
        "errors": errors[:20],
        "generated_at": iso_utc(),
    }


async def sync_players_for_all_leagues(conn: AsyncConnection) -> dict[str, Any]:
    """Upsert players and squad memberships for every catalog entry with API_FOOTBALL.

    Players API is paginated. We iterate pages until paging.current == paging.total.
    This is rate-limit aware: if no API key is configured the loop is skipped gracefully.
    """
    client = ApiFootballClient()
    total_players = 0
    errors: list[str] = []

    for entry in supported_competitions():
        league_id = entry.source.external_ids.get("API_FOOTBALL")
        if not league_id:
            continue
        if "players" not in (entry.source.capabilities.get("API_FOOTBALL") or []):
            continue

        season_year = (entry.starts_at or "")[:4]
        if not season_year.isdigit():
            continue

        # Get competition_season_id
        cs_row = await conn.execute(
            text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
            {"slug": entry.slug},
        )
        cs = cs_row.fetchone()
        if not cs:
            continue
        competition_season_id = cs[0]

        page = 1
        while True:
            try:
                data = await client.players(league=int(league_id), season=int(season_year), page=page)
            except Exception as exc:
                log.warning("player_sync: API failed league=%s page=%s err=%s", league_id, page, exc)
                errors.append(f"{entry.slug} page={page}: {exc}")
                break

            items = data.get("response") or []
            paging = data.get("paging") or {}

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
                        text("""
                            INSERT INTO players
                              (slug, display_name, nationality, date_of_birth,
                               position, number, photo_url, metadata)
                            VALUES
                              (:slug, :display_name, :nationality, cast(:dob as date),
                               :position, :number, :photo_url, cast(:metadata as jsonb))
                            ON CONFLICT (slug) DO UPDATE SET
                              display_name = excluded.display_name,
                              nationality = COALESCE(excluded.nationality, players.nationality),
                              date_of_birth = COALESCE(excluded.date_of_birth, players.date_of_birth),
                              position = COALESCE(excluded.position, players.position),
                              photo_url = COALESCE(excluded.photo_url, players.photo_url),
                              metadata = players.metadata || excluded.metadata,
                              updated_at = now()
                            RETURNING player_id::text
                        """),
                        {
                            "slug": player_slug or f"player-{external_id}",
                            "display_name": player_info.get("name") or f"{player_info.get('firstname','')} {player_info.get('lastname','')}".strip(),
                            "nationality": player_info.get("nationality"),
                            "dob": player_info.get("birth", {}).get("date"),
                            "position": player_info.get("position"),
                            "number": player_info.get("number"),
                            "photo_url": player_info.get("photo"),
                            "metadata": _json({"api_football_id": external_id}),
                        },
                    )
                    player_id = player_row.scalar_one()

                    # Squad link: find team_id by api_football team id
                    if team_info.get("id"):
                        team_ext = str(team_info["id"])
                        team_row = await conn.execute(
                            text("""
                                SELECT t.team_id::text FROM teams t
                                JOIN competition_season_teams cst ON cst.team_id = t.team_id
                                WHERE cst.competition_season_id = cast(:cs_id as uuid)
                                  AND cst.external_id = :ext_id
                                LIMIT 1
                            """),
                            {"cs_id": competition_season_id, "ext_id": team_ext},
                        )
                        team_link = team_row.fetchone()
                        if team_link:
                            await conn.execute(
                                text("""
                                    INSERT INTO squad_memberships
                                      (competition_season_id, team_id, player_id, position, shirt_number, source)
                                    VALUES
                                      (cast(:cs_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid),
                                       :position, :number, 'API_FOOTBALL')
                                    ON CONFLICT (competition_season_id, team_id, player_id) DO UPDATE SET
                                      position = COALESCE(excluded.position, squad_memberships.position),
                                      shirt_number = COALESCE(excluded.shirt_number, squad_memberships.shirt_number),
                                      updated_at = now()
                                """),
                                {
                                    "cs_id": competition_season_id,
                                    "team_id": team_link[0],
                                    "player_id": player_id,
                                    "position": player_info.get("position"),
                                    "number": player_info.get("number"),
                                },
                            )
                    total_players += 1
                except Exception as exc:
                    log.warning("player_sync: upsert failed slug=%s player=%s err=%s", entry.slug, player_info.get("name"), exc)
                    errors.append(f"{entry.slug}/{player_info.get('name')}: {exc}")

            if not items or paging.get("current") >= paging.get("total", 1):
                break
            page += 1

    status = "WARN" if errors else "OK"
    return {
        "status": status,
        "job_name": "sync_all_leagues_players",
        "records_processed": total_players,
        "errors": errors[:20],
        "generated_at": iso_utc(),
    }

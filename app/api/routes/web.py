import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import iso_utc
from app.db.repositories.published import PublishedRepository
from app.db.session import get_connection

router = APIRouter(prefix="/web", tags=["web"])

STAGE_LABELS = {
    "GROUP_STAGE": "Fase de grupos",
    "ROUND_OF_32": "Dieciseisavos",
    "ROUND_OF_16": "Octavos",
    "QUARTER_FINAL": "Cuartos",
    "SEMI_FINAL": "Semifinales",
    "THIRD_PLACE": "Tercer lugar",
    "FINAL": "Final",
}

SPORTING_ASSOCIATION_FLAGS = {
    "england": {"fifa_code": "ENG", "flag_emoji": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "flag_code": "ENG"},
    "inglaterra": {"fifa_code": "ENG", "flag_emoji": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "flag_code": "ENG"},
    "scotland": {"fifa_code": "SCO", "flag_emoji": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "flag_code": "SCO"},
    "escocia": {"fifa_code": "SCO", "flag_emoji": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "flag_code": "SCO"},
    "wales": {"fifa_code": "WAL", "flag_emoji": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "flag_code": "WAL"},
    "gales": {"fifa_code": "WAL", "flag_emoji": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "flag_code": "WAL"},
    "northern-ireland": {"fifa_code": "NIR", "flag_emoji": "🏴󠁧󠁢󠁮󠁩󠁲󠁿", "flag_code": "NIR"},
    "irlanda-del-norte": {"fifa_code": "NIR", "flag_emoji": "🏴󠁧󠁢󠁮󠁩󠁲󠁿", "flag_code": "NIR"},
}


def _serialize(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _serialize(value) for key, value in row.items()}


def _parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid datetime query parameter: {value}") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail="Datetime query parameters must include timezone information.")
    return parsed.astimezone(UTC)


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return _parse_utc_datetime(value)
    return None


def _slugish(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _normalize_stage_code(row: dict[str, Any]) -> str:
    raw_code = str(row.get("stage_code") or "").upper()
    if raw_code in STAGE_LABELS:
        return raw_code

    raw_name = str(row.get("stage_name") or "").upper()
    raw_type = str(row.get("stage_type") or "").upper()
    combined = f"{raw_code} {raw_name} {raw_type}"
    if raw_type in {"GROUP_STAGE", "LEAGUE_PHASE"}:
        return "GROUP_STAGE"
    if "THIRD" in combined or "TERCER" in combined:
        return "THIRD_PLACE"
    if "FINAL" in combined and "SEMI" not in combined and "QUARTER" not in combined:
        return "FINAL"
    if "SEMI" in combined:
        return "SEMI_FINAL"
    if "QUARTER" in combined or "CUART" in combined:
        return "QUARTER_FINAL"
    if "ROUND_OF_16" in combined or "ROUND OF 16" in combined or "OCTAV" in combined:
        return "ROUND_OF_16"
    if "ROUND_OF_32" in combined or "ROUND OF 32" in combined or "DIECISEIS" in combined:
        return "ROUND_OF_32"
    return "GROUP_STAGE"


def _is_knockout_row(row: dict[str, Any]) -> bool:
    view_type = str(row.get("stage_view_type") or "").upper()
    if view_type == "BRACKET_ROUND":
        return True
    if view_type in {"GROUP_TABLES", "LEAGUE_TABLE"}:
        return False
    stage_type = str(row.get("stage_type") or "").upper()
    if stage_type in {"GROUP_STAGE", "LEAGUE_PHASE"}:
        return False
    return _normalize_stage_code(row) != "GROUP_STAGE"


def _stage_label(stage_code: str, fallback: Any = None) -> str:
    return STAGE_LABELS.get(stage_code) or str(fallback or "").replace("_", " ").title() or "Partido"


def _group_label(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    normalized = re.sub(r"^(GROUP|GRUPO)[_\s-]*", "", raw, flags=re.IGNORECASE)
    if re.fullmatch(r"[A-Z]", normalized, flags=re.IGNORECASE):
        return f"Grupo {normalized.upper()}"
    if raw.lower().startswith("grupo "):
        return raw
    return raw.replace("_", " ")


def _slot_label(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    text = raw.replace("_", " ")
    match = re.match(r"Group\s+([A-L])\s+Winner", text, flags=re.IGNORECASE)
    if match:
        return f"Ganador Grupo {match.group(1).upper()}"
    match = re.match(r"Group\s+([A-L])\s+2(?:nd)?\s+Place", text, flags=re.IGNORECASE)
    if match:
        return f"2° Grupo {match.group(1).upper()}"
    match = re.match(r"Group\s+([A-L])\s+1(?:st)?\s+Place", text, flags=re.IGNORECASE)
    if match:
        return f"1° Grupo {match.group(1).upper()}"
    match = re.match(r"(?:Best|Mejor)\s+3(?:rd|°)?\s+Groups?\s+(.+)", text, flags=re.IGNORECASE)
    if match:
        groups = re.sub(r"[^A-L/]+", "", match.group(1).upper())
        return f"Mejor tercero {groups}" if groups else "Mejor tercero"
    replacements = [
        (r"Winner\s+Round\s+of\s+32\s*(\d*)", "Ganador dieciseisavos"),
        (r"Winner\s+Round\s+of\s+16\s*(\d*)", "Ganador octavos"),
        (r"Winner\s+Quarter(?:[-\s]?Final)?\s*(\d*)", "Ganador cuartos"),
        (r"Winner\s+Semi(?:[-\s]?Final)?\s*(\d*)", "Ganador semifinal"),
        (r"Loser\s+Semi(?:[-\s]?Final)?\s*(\d*)", "Perdedor semifinal"),
    ]
    for pattern, label in replacements:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            suffix = f" {match.group(1)}" if match.group(1) else ""
            return f"{label}{suffix}"
    return text


def _sporting_flag(row: dict[str, Any], side: str) -> dict[str, str | None]:
    metadata = row.get(f"{side}_team_metadata") if isinstance(row.get(f"{side}_team_metadata"), dict) else {}
    sports = metadata.get("sports") if isinstance(metadata.get("sports"), dict) else {}
    slug = _slugish(row.get(f"{side}_team_slug") or row.get(f"{side}_team_name"))
    inferred = SPORTING_ASSOCIATION_FLAGS.get(slug)
    fifa_code = sports.get("fifa_code") or (inferred or {}).get("fifa_code") or row.get(f"{side}_country_fifa_code")
    flag_code = sports.get("flag_code") or (inferred or {}).get("flag_code") or fifa_code or row.get(f"{side}_country_code")
    flag_asset = sports.get("flag_asset") or sports.get("flag_url")
    flag_emoji = sports.get("flag_emoji") or (inferred or {}).get("flag_emoji") or row.get(f"{side}_flag_emoji")
    return {"fifa_code": fifa_code, "flag_code": flag_code, "flag_asset": flag_asset, "flag_emoji": flag_emoji}


def _team_flag_from_fields(slug: Any, name: Any, country_code: Any, country_flag: Any, country_fifa_code: Any, metadata: Any) -> dict[str, str | None]:
    row = {
        "team_team_slug": slug,
        "team_team_name": name,
        "team_country_code": country_code,
        "team_flag_emoji": country_flag,
        "team_country_fifa_code": country_fifa_code,
        "team_team_metadata": metadata if isinstance(metadata, dict) else {},
    }
    return _sporting_flag(row, "team")


def _team_from_match_row(row: dict[str, Any], side: str) -> dict[str, Any] | None:
    team_id = row.get(f"{side}_team_id")
    slot_label = _slot_label(row.get(f"{side}_slot_label"))
    slot_code = row.get(f"{side}_slot_code")
    if not team_id and not slot_label:
        return None
    flags = _sporting_flag(row, side)
    display_name = row.get(f"{side}_team_name") or slot_label or "Por definir"
    return {
        "team_id": team_id,
        "slug": row.get(f"{side}_team_slug") or slot_code,
        "display_name": display_name,
        "country_code": row.get(f"{side}_country_code"),
        "fifa_code": flags.get("fifa_code"),
        "flag_code": flags.get("flag_code"),
        "flag_asset": flags.get("flag_asset"),
        "flag_emoji": flags.get("flag_emoji"),
        "is_placeholder": not bool(team_id),
        "participant_role": row.get(f"{side}_participant_role"),
        "slot_code": slot_code,
        "slot_label": slot_label,
    }


def _match_from_row(row: dict[str, Any]) -> dict[str, Any]:
    serialized = _serialize_row(row)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    weather = metadata.get("weather") or metadata.get("weather_snapshot") if isinstance(metadata, dict) else None
    stage_code = _normalize_stage_code(row)
    group_label = _group_label(row.get("group_name") or row.get("group_code"))
    return {
        "match_id": serialized.get("match_id"),
        "competition_season_slug": serialized.get("competition_season_slug"),
        "competition_name": serialized.get("competition_name"),
        "slug": serialized.get("slug"),
        "match_number": serialized.get("match_number"),
        "kickoff_at": serialized.get("kickoff_at"),
        "status": serialized.get("status"),
        "is_neutral": serialized.get("is_neutral"),
        "home_score": serialized.get("home_score"),
        "away_score": serialized.get("away_score"),
        "winner_team_id": serialized.get("winner_team_id"),
        "stage_code": stage_code,
        "source_stage_code": serialized.get("stage_code"),
        "stage_id": serialized.get("stage_id"),
        "stage_view_type": serialized.get("stage_view_type"),
        "stage_rules": serialized.get("stage_rules"),
        "stage_name": serialized.get("stage_name"),
        "stage_label": _stage_label(stage_code, serialized.get("stage_name")),
        "stage_type": serialized.get("stage_type"),
        "group_id": serialized.get("group_id"),
        "group_code": serialized.get("group_code"),
        "group_name": serialized.get("group_name"),
        "group_label": group_label,
        "group_order": serialized.get("group_order"),
        "home": _team_from_match_row(row, "home"),
        "away": _team_from_match_row(row, "away"),
        "venue": {
            "venue_id": serialized.get("venue_id"),
            "slug": serialized.get("venue_slug"),
            "display_name": serialized.get("venue_name"),
            "city": serialized.get("venue_city"),
            "country_code": serialized.get("venue_country_code"),
            "flag_emoji": serialized.get("venue_flag_emoji"),
            "timezone_name": serialized.get("venue_timezone"),
            "latitude": serialized.get("venue_latitude"),
            "longitude": serialized.get("venue_longitude"),
        },
        "weather": weather,
        "metadata": metadata,
    }


@router.get("/matches")
async def web_matches(
    season: str | None = None,
    kickoff_from: str | None = None,
    kickoff_to: str | None = None,
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    settings = get_settings()
    repo = PublishedRepository(conn)
    rows = await repo.match_schedule(
        season or settings.default_season_slug,
        _parse_utc_datetime(kickoff_from),
        _parse_utc_datetime(kickoff_to),
    )
    data = {
        "season": {"slug": season or settings.default_season_slug},
        "matches": [_match_from_row(row) for row in rows],
        "generated_at": iso_utc(),
    }
    return {"ok": True, "data": data}


@router.get("/matches-overview")
async def web_matches_overview(
    season: str | None = None,
    yesterday_from: str | None = None,
    yesterday_to: str | None = None,
    today_from: str | None = None,
    today_to: str | None = None,
    tomorrow_from: str | None = None,
    tomorrow_to: str | None = None,
    upcoming_from: str | None = None,
    upcoming_to: str | None = None,
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    settings = get_settings()
    season_slug = season or settings.default_season_slug
    bounds = [
        _parse_utc_datetime(v)
        for v in (yesterday_from, yesterday_to, today_from, today_to, tomorrow_from, tomorrow_to, upcoming_from, upcoming_to)
        if v
    ]
    kickoff_from = min(bounds) if bounds else None
    kickoff_to = max(bounds) if bounds else None
    repo = PublishedRepository(conn)
    rows = [_match_from_row(row) for row in await repo.match_schedule(season_slug, kickoff_from, kickoff_to)]

    def in_range(row: dict[str, Any], start: str | None, end: str | None) -> bool:
        if not start and not end:
            return False
        kickoff = _to_datetime(row.get("kickoff_at"))
        start_dt = _parse_utc_datetime(start)
        end_dt = _parse_utc_datetime(end)
        return bool(kickoff) and (not start_dt or kickoff >= start_dt) and (not end_dt or kickoff < end_dt)

    data = {
        "season": {"slug": season_slug},
        "yesterday": [row for row in rows if in_range(row, yesterday_from, yesterday_to)],
        "today": [row for row in rows if in_range(row, today_from, today_to)],
        "tomorrow": [row for row in rows if in_range(row, tomorrow_from, tomorrow_to)],
        "upcoming": [row for row in rows if in_range(row, upcoming_from, upcoming_to)],
        "ranges": {
            "yesterday": {"from": yesterday_from, "to": yesterday_to},
            "today": {"from": today_from, "to": today_to},
            "tomorrow": {"from": tomorrow_from, "to": tomorrow_to},
            "upcoming": {"from": upcoming_from or tomorrow_from, "to": upcoming_to},
        },
        "generated_at": iso_utc(),
    }
    return {"ok": True, "data": data}


@router.get("/standings")
async def web_standings(season: str | None = None, conn: AsyncConnection = Depends(get_connection)) -> dict:
    settings = get_settings()
    season_slug = season or settings.default_season_slug
    repo = PublishedRepository(conn)
    rows = await repo.standings_groups(season_slug)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        flags = _team_flag_from_fields(
            row.get("team_slug"),
            row.get("team_name"),
            row.get("team_country_code"),
            row.get("flag_emoji"),
            row.get("country_fifa_code"),
            row.get("team_metadata"),
        )
        row = {**row, "flag_emoji": flags.get("flag_emoji"), "fifa_code": flags.get("fifa_code"), "flag_code": flags.get("flag_code")}
        group_id = row["group_id"]
        grouped.setdefault(
            group_id,
            {
                "group_id": group_id,
                "group_code": row["group_code"],
                "group_name": row["group_name"],
                "group_order": row["group_order"],
                "standings": [],
            },
        )
        if row.get("team_id"):
            grouped[group_id]["standings"].append(_serialize_row(row))
    return {"ok": True, "data": {"season": {"slug": season_slug}, "groups": list(grouped.values()), "generated_at": iso_utc()}}


@router.get("/teams")
async def web_teams(season: str | None = None, conn: AsyncConnection = Depends(get_connection)) -> dict:
    standings_response = await web_standings(season, conn)
    standings = standings_response["data"]
    teams = []
    for group in standings["groups"]:
        for row in group["standings"]:
            teams.append(
                {
                    "team_id": row["team_id"],
                    "team_slug": row["team_slug"],
                    "team_name": row["team_name"],
                    "slug": row["team_slug"],
                    "display_name": row["team_name"],
                    "flag_emoji": row["flag_emoji"],
                    "fifa_code": row.get("fifa_code"),
                    "flag_code": row.get("flag_code"),
                    "country_code": row.get("team_country_code"),
                    "group_code": group["group_code"],
                    "group_name": group["group_name"],
                    "position": row["position"],
                    "points": row["points"],
                    "played": row["played"],
                    "wins": row["wins"],
                    "draws": row["draws"],
                    "losses": row["losses"],
                    "goals_for": row["goals_for"],
                    "goals_against": row["goals_against"],
                    "goal_difference": row["goal_difference"],
                }
            )
    return {"ok": True, "data": {"season": standings["season"], "groups": standings["groups"], "teams": teams, "generated_at": iso_utc()}}


@router.get("/team-detail")
async def web_team_detail(team_slug: str = Query(...), season: str | None = None, conn: AsyncConnection = Depends(get_connection)) -> dict:
    repo = PublishedRepository(conn)
    season_slug = season or get_settings().default_season_slug
    team = await repo.fetch_one(
        """
        select t.*, c.flag_emoji, c.fifa_code as country_fifa_code
        from teams t
        left join countries c on c.code_alpha2 = t.country_code
        where t.slug = :team_slug
        """,
        {"team_slug": team_slug},
    )
    if not team:
        return {"ok": True, "data": {"team": None, "matches": [], "roster": [], "generated_at": iso_utc()}}
    team_id = str(team["team_id"])
    matches = await repo.match_schedule_for_team(season_slug, team_id)
    roster = await repo.fetch_all(
        """
        select
          p.player_id::text,
          p.slug,
          p.display_name,
          cr.position,
          cr.shirt_number,
          jsonb_build_object(
            'appearances', count(distinct pms.match_id) filter (where pms.stat_name = 'minutes'),
            'minutes', coalesce(sum(pms.stat_value) filter (where pms.stat_name = 'minutes'), 0),
            'goals', coalesce(sum(pms.stat_value) filter (where pms.stat_name = 'goals_scored'), 0),
            'assists', coalesce(sum(pms.stat_value) filter (where pms.stat_name = 'assists'), 0),
            'yellow_cards', coalesce(sum(pms.stat_value) filter (where pms.stat_name = 'yellow_cards'), 0),
            'red_cards', coalesce(sum(pms.stat_value) filter (where pms.stat_name = 'red_cards'), 0),
            'avg_rating', round(avg(pms.stat_value) filter (where pms.stat_name = 'rating'), 2)
          ) as stats
        from competition_rosters cr
        join players p on p.player_id = cr.player_id
        join competition_seasons cs on cs.competition_season_id = cr.competition_season_id
        left join player_match_stats pms
          on pms.player_id = cr.player_id
         and pms.team_id = cr.team_id
         and pms.match_id in (
           select m.match_id from matches m
           where m.competition_season_id = cr.competition_season_id
         )
        where cs.slug = :season and cr.team_id = :team_id
        group by p.player_id, p.slug, p.display_name, cr.position, cr.shirt_number
        order by cr.position nulls last, p.display_name
        """,
        {"season": season_slug, "team_id": team["team_id"]},
    )
    flags = _team_flag_from_fields(
        team.get("slug"),
        team.get("display_name"),
        team.get("country_code"),
        team.get("flag_emoji"),
        team.get("country_fifa_code"),
        team.get("metadata"),
    )
    team_payload = {**_serialize_row(team), **flags}
    data = {
        "team": team_payload,
        "matches": [_match_from_row(r) for r in matches],  # already filtered by team via SQL
        "roster": [_serialize_row(r) for r in roster],
        "generated_at": iso_utc(),
    }
    return {"ok": True, "data": data}


@router.get("/knockout")
async def web_knockout(season: str | None = None, conn: AsyncConnection = Depends(get_connection)) -> dict:
    repo = PublishedRepository(conn)
    season_slug = season or get_settings().default_season_slug
    rows = await repo.match_schedule_knockout(season_slug)
    by_round: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        match = _match_from_row(row)
        by_round[str(match["stage_code"])].append(match)
    data = {"season": {"slug": season_slug}, "rounds": dict(by_round), "matches": [_match_from_row(r) for r in rows], "generated_at": iso_utc()}
    return {"ok": True, "data": data}

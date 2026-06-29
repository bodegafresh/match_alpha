import asyncio
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
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

_SUPPORTED_LANGS = {"es", "en"}
_SPANISH_TIMEZONES = {"America/Santiago", "America/Punta_Arenas"}


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


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_lang(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    base = cleaned.split(",", 1)[0].split("-", 1)[0].split("_", 1)[0]
    return base if base in _SUPPORTED_LANGS else None


def _lang_from_accept_language(value: str | None) -> str | None:
    if not value:
        return None
    for token in value.split(","):
        lang = _normalize_lang(token.split(";", 1)[0])
        if lang:
            return lang
    return None


def _lang_from_timezone(value: str | None) -> str | None:
    if not value:
        return None
    tz = str(value).strip()
    if tz in _SPANISH_TIMEZONES:
        return "es"
    return None


def _resolve_lang(lang: str | None, timezone: str | None, accept_language: str | None) -> str:
    return (
        _normalize_lang(lang)
        or _lang_from_accept_language(accept_language)
        or _lang_from_timezone(timezone)
        or "en"
    )


def _localized_team_name(default_name: Any, metadata: Any, lang: str) -> str:
    names = _as_dict(_as_dict(metadata).get("names"))
    fallbacks: list[str] = []
    if lang:
        fallbacks.append(lang)
    if lang != "en":
        fallbacks.append("en")
    if lang != "es":
        fallbacks.append("es")
    for key in fallbacks:
        value = names.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(default_name or "Por definir")


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


def _team_from_match_row(row: dict[str, Any], side: str, lang: str = "en") -> dict[str, Any] | None:
    team_id = row.get(f"{side}_team_id")
    slot_label = _slot_label(row.get(f"{side}_slot_label"))
    slot_code = row.get(f"{side}_slot_code")
    if not team_id and not slot_label:
        return None
    flags = _sporting_flag(row, side)
    metadata = row.get(f"{side}_team_metadata")
    display_name = _localized_team_name(row.get(f"{side}_team_name"), metadata, lang) if team_id else (slot_label or "Por definir")
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


def _match_from_row(row: dict[str, Any], lang: str = "en") -> dict[str, Any]:
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
        "home": _team_from_match_row(row, "home", lang=lang),
        "away": _team_from_match_row(row, "away", lang=lang),
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


import json as _json_mod

# Weather is fetched at most once per match per WEATHER_CACHE_TTL_HOURS.
# After fetch it is persisted in matches.metadata so subsequent requests
# read from DB instead of calling the external API.
_WEATHER_CACHE_TTL_HOURS = 3
# Only enrich matches that kick off within this window (no point fetching
# weather for a match that is 7 days away).  WeatherAPI free tier supports
# forecasts up to 3 days ahead.
_WEATHER_LOOKAHEAD_HOURS = 48


async def _fetch_weather_for_match(
    city: str | None,
    country: str | None,
    kickoff_at: str | None,
) -> dict:
    """Fetch forecast weather at match kickoff time from WeatherAPI.

    Uses forecast.json (not current.json) so the temperature/rain/wind
    returned reflects conditions at the actual kickoff hour, not now.
    Falls back to current.json if kickoff is in the past or unparseable.
    """
    settings = get_settings()
    if not settings.weather_api_key or not city:
        return {}
    import httpx

    location = f"{city},{country or ''}".strip(",")

    # Determine kickoff date and hour for the forecast query
    kickoff_dt: datetime | None = None
    if kickoff_at:
        try:
            kickoff_dt = datetime.fromisoformat(str(kickoff_at).replace("Z", "+00:00"))
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            if kickoff_dt and kickoff_dt > datetime.now(UTC):
                # Use forecast endpoint — returns hourly data up to 3 days ahead
                r = await client.get(
                    "https://api.weatherapi.com/v1/forecast.json",
                    params={
                        "key": settings.weather_api_key,
                        "q": location,
                        "dt": kickoff_dt.strftime("%Y-%m-%d"),
                        "aqi": "no",
                        "alerts": "no",
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    # Extract the hourly slot closest to kickoff
                    forecast_day = (data.get("forecast", {}).get("forecastday") or [{}])[0]
                    hours = forecast_day.get("hour") or []
                    kickoff_hour = kickoff_dt.hour
                    # Pick exact hour or nearest available
                    hour_data = next(
                        (h for h in hours if datetime.fromisoformat(h["time"]).hour == kickoff_hour),
                        hours[kickoff_hour] if hours and kickoff_hour < len(hours) else (hours[-1] if hours else {}),
                    )
                    return {
                        "temperature_c": hour_data.get("temp_c"),
                        "condition": hour_data.get("condition", {}).get("text"),
                        "wind_kph": hour_data.get("wind_kph"),
                        "humidity_pct": hour_data.get("humidity"),
                        "precip_mm": hour_data.get("precip_mm"),
                        "chance_of_rain": hour_data.get("chance_of_rain"),
                        "kickoff_at": kickoff_at,
                        "forecast_type": "kickoff_hour",
                        "fetched_at": iso_utc(),
                    }
            # Fallback: current conditions (match already started or kickoff unparseable)
            r = await client.get(
                "https://api.weatherapi.com/v1/current.json",
                params={"key": settings.weather_api_key, "q": location, "aqi": "no"},
            )
            if r.status_code == 200:
                current = r.json().get("current", {})
                return {
                    "temperature_c": current.get("temp_c"),
                    "condition": current.get("condition", {}).get("text"),
                    "wind_kph": current.get("wind_kph"),
                    "humidity_pct": current.get("humidity"),
                    "precip_mm": current.get("precip_mm"),
                    "forecast_type": "current",
                    "fetched_at": iso_utc(),
                }
    except Exception:
        pass
    return {}


def _weather_is_fresh(weather: dict | None) -> bool:
    """True if cached weather was fetched within TTL and can be reused."""
    if not weather:
        return False
    fetched_at_str = weather.get("fetched_at")
    if not fetched_at_str:
        return False
    try:
        fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
        age_hours = (datetime.now(UTC) - fetched_at).total_seconds() / 3600
        return age_hours < _WEATHER_CACHE_TTL_HOURS
    except Exception:
        return False


async def _persist_weather(conn: AsyncConnection, match_id: str, weather: dict) -> None:
    """Persist fetched weather into matches.metadata to avoid future API calls."""
    try:
        await conn.execute(
            text("""
                UPDATE matches
                SET metadata = COALESCE(metadata, '{}'::jsonb) || cast(:weather_patch as jsonb),
                    updated_at = now()
                WHERE match_id = cast(:match_id as uuid)
            """),
            {
                "match_id": match_id,
                "weather_patch": _json_mod.dumps({"weather": weather}),
            },
        )
    except Exception:
        pass  # Weather persistence is best-effort


async def _enrich_weather(
    matches: list[dict],
    conn: AsyncConnection | None = None,
    limit: int = 8,
) -> list[dict]:
    """Fetch weather for upcoming SCHEDULED matches that lack fresh cached data.

    Rules:
    - Only matches kicking off within the next _WEATHER_LOOKAHEAD_HOURS hours.
    - Skip matches that already have fresh weather (within TTL).
    - At most `limit` API calls per request (default 8, matching frontend param).
    - Persist results to DB so next request reads from metadata.
    """
    now = datetime.now(UTC)
    lookahead_cutoff = now.replace(tzinfo=UTC)

    needs_weather: list[dict] = []
    for m in matches:
        # Already have fresh weather → skip
        if _weather_is_fresh(m.get("weather")):
            continue
        # Only SCHEDULED matches within lookahead window
        kickoff_str = m.get("kickoff_at")
        if not kickoff_str:
            continue
        try:
            kickoff = datetime.fromisoformat(str(kickoff_str).replace("Z", "+00:00"))
        except Exception:
            continue
        hours_until = (kickoff - lookahead_cutoff).total_seconds() / 3600
        if hours_until < -2 or hours_until > _WEATHER_LOOKAHEAD_HOURS:
            continue
        if not m.get("venue", {}).get("city"):
            continue
        if m.get("status") == "FINISHED":
            continue
        needs_weather.append(m)

    # Respect caller limit
    needs_weather = needs_weather[:limit]
    if not needs_weather:
        return matches

    results = await asyncio.gather(*[
        _fetch_weather_for_match(
            m["venue"]["city"],
            m["venue"].get("country_code"),
            m.get("kickoff_at"),
        )
        for m in needs_weather
    ], return_exceptions=True)

    persist_tasks = []
    weather_map: dict[str, dict] = {}
    for m, r in zip(needs_weather, results):
        if isinstance(r, dict) and r:
            weather_map[m["match_id"]] = r
            if conn and m.get("match_id"):
                persist_tasks.append(_persist_weather(conn, m["match_id"], r))

    if persist_tasks:
        await asyncio.gather(*persist_tasks, return_exceptions=True)

    for m in matches:
        if m["match_id"] in weather_map:
            m["weather"] = weather_map[m["match_id"]]
    return matches


@router.get("/matches")
async def web_matches(
    season: str | None = None,
    kickoff_from: str | None = None,
    kickoff_to: str | None = None,
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    settings = get_settings()
    repo = PublishedRepository(conn)
    rows = await repo.match_schedule(
        season or settings.default_season_slug,
        _parse_utc_datetime(kickoff_from),
        _parse_utc_datetime(kickoff_to),
    )
    preferred_lang = _resolve_lang(lang, timezone, accept_language)
    matches = await _enrich_weather([_match_from_row(row, lang=preferred_lang) for row in rows], conn=conn)
    data = {
        "season": {"slug": season or settings.default_season_slug},
        "matches": matches,
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
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    weather_refresh_limit: int = Query(default=8, ge=0, le=20),
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
    preferred_lang = _resolve_lang(lang, timezone, accept_language)
    rows = await _enrich_weather(
        [_match_from_row(row, lang=preferred_lang) for row in await repo.match_schedule(season_slug, kickoff_from, kickoff_to)],
        conn=conn,
        limit=weather_refresh_limit,
    )

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
async def web_standings(
    season: str | None = None,
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    settings = get_settings()
    season_slug = season or settings.default_season_slug
    preferred_lang = _resolve_lang(lang, timezone, accept_language)
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
        row["team_name"] = _localized_team_name(row.get("team_name"), row.get("team_metadata"), preferred_lang)
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
    groups_list = sorted(grouped.values(), key=lambda g: g.get("group_order") or 0)
    for g in groups_list:
        g["standings"].sort(key=lambda r: r.get("position") or 99)
    return {"ok": True, "data": {"season": {"slug": season_slug}, "groups": groups_list, "generated_at": iso_utc()}}


@router.get("/teams")
async def web_teams(
    season: str | None = None,
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    standings_response = await web_standings(
        season=season,
        lang=lang,
        timezone=timezone,
        accept_language=accept_language,
        conn=conn,
    )
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
async def web_team_detail(
    team_slug: str = Query(...),
    season: str | None = None,
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    repo = PublishedRepository(conn)
    season_slug = season or get_settings().default_season_slug
    preferred_lang = _resolve_lang(lang, timezone, accept_language)
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
    team_payload = {
        **_serialize_row(team),
        **flags,
        "display_name": _localized_team_name(team.get("display_name"), team.get("metadata"), preferred_lang),
    }
    data = {
        "team": team_payload,
        "matches": [_match_from_row(r, lang=preferred_lang) for r in matches],  # already filtered by team via SQL
        "roster": [_serialize_row(r) for r in roster],
        "generated_at": iso_utc(),
    }
    return {"ok": True, "data": data}


@router.get("/knockout")
async def web_knockout(
    season: str | None = None,
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    repo = PublishedRepository(conn)
    season_slug = season or get_settings().default_season_slug
    preferred_lang = _resolve_lang(lang, timezone, accept_language)
    rows = await repo.match_schedule_knockout(season_slug)
    by_round: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        match = _match_from_row(row, lang=preferred_lang)
        by_round[str(match["stage_code"])].append(match)
    data = {
        "season": {"slug": season_slug},
        "rounds": dict(by_round),
        "matches": [_match_from_row(r, lang=preferred_lang) for r in rows],
        "generated_at": iso_utc(),
    }
    return {"ok": True, "data": data}


# ─── News ─────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# News ingest (called by GAS after fetching RSS)
# ---------------------------------------------------------------------------

class NewsItemIn(BaseModel):
    id_hash: str
    match_id: str | None = None
    home_team: str
    away_team: str
    title: str
    url: str
    source: str = "Google News RSS"
    pub_date: str | None = None  # ISO or RFC 2822 string


@router.post("/news/ingest")
async def news_ingest(
    items: list[NewsItemIn],
    x_internal_key: str | None = Header(default=None, alias="X-Internal-Key"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Receive news items from GAS and upsert into news_items table.
    Authenticated with X-Internal-Key header."""
    settings = get_settings()
    if x_internal_key != settings.api_internal_key:
        raise HTTPException(status_code=401, detail="Invalid internal key")
    if not items:
        return {"ok": True, "inserted": 0}

    inserted = 0
    for item in items:
        # Resolve match_id from home/away team names if not provided
        match_id = item.match_id
        if not match_id:
            res = await conn.execute(
                text("""
                    SELECT m.match_id::text
                    FROM matches m
                    JOIN match_participants hp ON hp.match_id = m.match_id AND hp.side = 'HOME'
                    JOIN teams ht ON ht.team_id = hp.team_id
                    JOIN match_participants ap ON ap.match_id = m.match_id AND ap.side = 'AWAY'
                    JOIN teams away_t ON away_t.team_id = ap.team_id
                    WHERE ht.display_name ILIKE :home
                      AND away_t.display_name ILIKE :away
                      AND m.kickoff_at::date >= CURRENT_DATE - interval '1 day'
                    ORDER BY m.kickoff_at ASC
                    LIMIT 1
                """),
                {"home": f"%{item.home_team}%", "away": f"%{item.away_team}%"},
            )
            row = res.fetchone()
            match_id = row[0] if row else None

        import uuid as _uuid
        from email.utils import parsedate_to_datetime as _parse_rfc2822

        pub_date_val: datetime | None = None
        if item.pub_date:
            try:
                pub_date_val = _parse_rfc2822(item.pub_date)
            except Exception:
                try:
                    pub_date_val = datetime.fromisoformat(item.pub_date.replace("Z", "+00:00"))
                except Exception:
                    pass

        match_id_val = None
        if match_id:
            try:
                match_id_val = _uuid.UUID(match_id)
            except Exception:
                match_id_val = None

        result = await conn.execute(
            text("""
                INSERT INTO news_items (id_hash, match_id, home_team, away_team,
                                        title, url, source, pub_date)
                VALUES (:id_hash, :match_id, :home_team, :away_team,
                        :title, :url, :source, :pub_date)
                ON CONFLICT (id_hash) DO NOTHING
            """),
            {
                "id_hash": item.id_hash,
                "match_id": match_id_val,
                "home_team": item.home_team,
                "away_team": item.away_team,
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "pub_date": pub_date_val,
            },
        )
        inserted += result.rowcount

    await conn.commit()
    return {"ok": True, "inserted": inserted, "total": len(items)}


# ---------------------------------------------------------------------------
# News read (frontend + internal)
# ---------------------------------------------------------------------------

@router.get("/news")
async def web_news(
    season: str | None = None,
    lang: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Today's matches with team names, AI context flag, and cached news from news_items."""
    season_slug = season or get_settings().default_season_slug
    preferred_lang = _resolve_lang(lang, timezone, accept_language)

    rows = await conn.execute(
        text("""
            SELECT
              m.match_id::text,
              m.kickoff_at,
              m.status,
              cs.stage_code,
              home_t.display_name AS home_team,
              home_t.metadata AS home_team_metadata,
              away_t.display_name AS away_team,
              away_t.metadata AS away_team_metadata,
              m.home_score,
              m.away_score
            FROM matches m
            JOIN competition_seasons cs2 ON cs2.competition_season_id = m.competition_season_id
            JOIN competition_stages cs ON cs.stage_id = m.stage_id
            JOIN match_participants hp ON hp.match_id = m.match_id AND hp.side = 'HOME'
            JOIN teams home_t ON home_t.team_id = hp.team_id
            JOIN match_participants ap ON ap.match_id = m.match_id AND ap.side = 'AWAY'
            JOIN teams away_t ON away_t.team_id = ap.team_id
            WHERE cs2.slug = :season
              AND m.kickoff_at >= now() - interval '4 hours'
              AND m.kickoff_at <  now() + interval '28 hours'
              AND m.status != 'CANCELLED'
            ORDER BY m.kickoff_at ASC
        """),
        {"season": season_slug},
    )
    today_matches = []
    for r in rows:
        item = dict(r._mapping)
        item["home_team"] = _localized_team_name(item.get("home_team"), item.get("home_team_metadata"), preferred_lang)
        item["away_team"] = _localized_team_name(item.get("away_team"), item.get("away_team_metadata"), preferred_lang)
        today_matches.append(item)

    if not today_matches:
        return {"ok": True, "data": {"matches_news": [], "generated_at": iso_utc()}}

    import uuid as _uuid
    match_ids = [m["match_id"] for m in today_matches]
    match_uuids = [_uuid.UUID(mid) for mid in match_ids]

    # AI context: matches that had AI adjustment applied
    ai_rows = await conn.execute(
        text("""
            SELECT DISTINCT mp.match_id::text
            FROM model_predictions mp
            WHERE mp.match_id = ANY(:ids)
              AND mp.explanation::text ILIKE '%ai_adjustment%'
        """),
        {"ids": match_uuids},
    )
    ai_adjusted_ids = {r[0] for r in ai_rows}

    # News from cache
    news_rows = await conn.execute(
        text("""
            SELECT match_id::text, title, url, source,
                   pub_date, home_team, away_team
            FROM news_items
            WHERE match_id = ANY(:ids)
            ORDER BY pub_date DESC NULLS LAST
        """),
        {"ids": match_uuids},
    )
    news_by_match: dict[str, list[dict]] = defaultdict(list)
    for r in news_rows:
        news_by_match[r.match_id].append({
            "title": r.title,
            "url": r.url,
            "source": r.source,
            "published_at": iso_utc(r.pub_date) if r.pub_date else None,
            "home_team": r.home_team,
            "away_team": r.away_team,
        })

    matches_news = [
        {
            "match_id": m["match_id"],
            "kickoff_at": iso_utc(m["kickoff_at"]),
            "status": m["status"],
            "stage_code": m["stage_code"],
            "home_team": m["home_team"],
            "away_team": m["away_team"],
            "home_score": m["home_score"],
            "away_score": m["away_score"],
            "ai_context_used": m["match_id"] in ai_adjusted_ids,
            "news": news_by_match.get(m["match_id"], [])[:10],
        }
        for m in today_matches
    ]

    return {"ok": True, "data": {"matches_news": matches_news, "generated_at": iso_utc()}}

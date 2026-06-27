from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.clients.api_football_client import ApiFootballClient
from app.clients.espn_client import EspnClient
from app.clients.football_data_client import FootballDataClient
from app.clients.sportmonks_client import SportmonksClient
from app.competitions.catalog import CompetitionCatalogEntry, get_catalog_entry, supported_competitions
from app.core.config import get_settings
from app.core.hashing import sha256_json
from app.core.time import iso_utc, utc_now
from app.db.repositories.observability import ObservabilityRepository
from app.normalization.competition_format import get_format_normalizer

logger = logging.getLogger(__name__)


def _json(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _parse_iso_dt(value: Any) -> datetime | None:
    """Parses an ISO 8601 string to an aware datetime. asyncpg requires datetime objects, not strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


TEAM_ALIASES = {
    "alemania": "germany",
    "costa de marfil": "cote divoire",
    "cote divoire": "cote divoire",
    "cote d ivoire": "cote divoire",
    "ee uu": "united states",
    "estados unidos": "united states",
    "japon": "japan",
    "paises bajos": "netherlands",
    "suecia": "sweden",
    "tunez": "tunisia",
    "turquia": "turkey",
}


def _normalize_name(value: Any) -> str:
    text_value = unicodedata.normalize("NFD", str(value or ""))
    text_value = "".join(ch for ch in text_value if unicodedata.category(ch) != "Mn")
    text_value = text_value.replace("&", " and ")
    text_value = re.sub(r"[^a-zA-Z0-9]+", " ", text_value).strip().lower()
    text_value = re.sub(r"\s+", " ", text_value)
    return TEAM_ALIASES.get(text_value, text_value)


def _slug(value: Any) -> str:
    return _normalize_name(value).replace(" ", "-")


def _date_ymd(value: str | None) -> str:
    return str(value or "")[:10]


async def seed_competition_catalog(conn: AsyncConnection, competition: str | None = None) -> dict[str, Any]:
    entries = [get_catalog_entry(competition)] if competition else supported_competitions()
    seeded: list[str] = []
    for entry in entries:
        normalizer = get_format_normalizer(entry.format_code)
        plan = normalizer.build_plan(entry)
        competition_id = await _upsert_competition(conn, entry)
        season_id = await _upsert_season(conn, entry, competition_id, plan)
        stage_ids = await _upsert_stages(conn, entry, season_id)
        await _upsert_groups(conn, entry, season_id, stage_ids)
        await _upsert_status(conn, season_id)
        await _upsert_source_external_refs(conn, entry, season_id)
        seeded.append(entry.slug)
    return {"status": "OK", "job_name": "seed_competition_catalog", "records_processed": len(seeded), "seeded": seeded}


async def _upsert_competition(conn: AsyncConnection, entry: CompetitionCatalogEntry) -> str:
    row = await conn.execute(
        text(
            """
            insert into competitions
              (slug, display_name, competition_type, country_code, region, tier, is_international, metadata)
            values
              (:slug, :display_name, cast(:competition_type as competition_type), :country_code, :region, :tier,
               :is_international, cast(:metadata as jsonb))
            on conflict (slug) do update set
              display_name = excluded.display_name,
              competition_type = excluded.competition_type,
              country_code = excluded.country_code,
              region = excluded.region,
              tier = excluded.tier,
              is_international = excluded.is_international,
              metadata = competitions.metadata || excluded.metadata,
              updated_at = now()
            returning competition_id::text
            """,
        ),
        {
            "slug": entry.competition_slug,
            "display_name": entry.name,
            "competition_type": entry.competition_type,
            "country_code": entry.country_code,
            "region": entry.region,
            "tier": entry.tier,
            "is_international": entry.is_international,
            "metadata": _json(entry.competition_metadata),
        },
    )
    return row.scalar_one()


async def _upsert_season(
    conn: AsyncConnection,
    entry: CompetitionCatalogEntry,
    competition_id: str,
    plan: Any,
) -> str:
    metadata = entry.season_metadata | {
        "normalizer": {
            "format_code": plan.format_code,
            "default_stage_code": plan.default_stage_code,
            "has_groups": plan.has_groups,
            "has_league_table": plan.has_league_table,
            "has_knockout": plan.has_knockout,
        }
    }
    row = await conn.execute(
        text(
            """
            insert into competition_seasons
              (competition_id, slug, season_label, starts_at, ends_at, timezone_name, status, format_code, metadata)
            values
              (cast(:competition_id as uuid), :slug, :season_label, cast(:starts_at as timestamptz),
               cast(:ends_at as timestamptz), :timezone_name, 'SCHEDULED', :format_code, cast(:metadata as jsonb))
            on conflict (slug) do update set
              competition_id = excluded.competition_id,
              season_label = excluded.season_label,
              starts_at = excluded.starts_at,
              ends_at = excluded.ends_at,
              timezone_name = excluded.timezone_name,
              format_code = excluded.format_code,
              metadata = competition_seasons.metadata || excluded.metadata,
              updated_at = now()
            returning competition_season_id::text
            """,
        ),
        {
            "competition_id": competition_id,
            "slug": entry.slug,
            "season_label": entry.season_label,
            "starts_at": _parse_iso_dt(entry.starts_at),
            "ends_at": _parse_iso_dt(entry.ends_at),
            "timezone_name": entry.timezone_name,
            "format_code": entry.format_code,
            "metadata": _json(metadata),
        },
    )
    return row.scalar_one()


async def _upsert_stages(conn: AsyncConnection, entry: CompetitionCatalogEntry, season_id: str) -> dict[str, str]:
    stage_ids: dict[str, str] = {}
    for stage in entry.stages:
        row = await conn.execute(
            text(
                """
                insert into competition_stages
                  (competition_season_id, stage_code, stage_name, stage_order, stage_type, rules)
                values
                  (cast(:season_id as uuid), :stage_code, :stage_name, :stage_order,
                   cast(:stage_type as stage_type), cast(:rules as jsonb))
                on conflict (competition_season_id, stage_code) do update set
                  stage_name = excluded.stage_name,
                  stage_order = excluded.stage_order,
                  stage_type = excluded.stage_type,
                  rules = competition_stages.rules || excluded.rules,
                  updated_at = now()
                returning stage_id::text
                """,
            ),
            {
                "season_id": season_id,
                "stage_code": stage.stage_code,
                "stage_name": stage.stage_name,
                "stage_order": stage.stage_order,
                "stage_type": stage.stage_type,
                "rules": _json(stage.rules),
            },
        )
        stage_ids[stage.stage_code] = row.scalar_one()
    return stage_ids


async def _upsert_groups(
    conn: AsyncConnection,
    entry: CompetitionCatalogEntry,
    season_id: str,
    stage_ids: dict[str, str],
) -> None:
    if not entry.groups:
        return
    group_stage_id = stage_ids.get("GROUP_STAGE")
    if not group_stage_id:
        return
    for group in entry.groups:
        await conn.execute(
            text(
                """
                insert into competition_groups
                  (competition_season_id, stage_id, group_code, group_name, group_order, metadata)
                values
                  (cast(:season_id as uuid), cast(:stage_id as uuid), :group_code, :group_name, :group_order,
                   cast(:metadata as jsonb))
                on conflict (competition_season_id, stage_id, group_code) do update set
                  group_name = excluded.group_name,
                  group_order = excluded.group_order,
                  metadata = competition_groups.metadata || excluded.metadata,
                  updated_at = now()
                """,
            ),
            {
                "season_id": season_id,
                "stage_id": group_stage_id,
                "group_code": group.group_code,
                "group_name": group.group_name,
                "group_order": group.group_order,
                "metadata": _json({"source": "competition_catalog"}),
            },
        )


async def _upsert_status(conn: AsyncConnection, season_id: str) -> None:
    await conn.execute(
        text(
            """
            insert into competition_status (competition_season_id, status, status_reason, readiness_score)
            values (cast(:season_id as uuid), 'OBSERVATION', 'Catalog seeded; readiness checks pending.', 0)
            on conflict (competition_season_id) do nothing
            """,
        ),
        {"season_id": season_id},
    )


async def _upsert_source_external_refs(conn: AsyncConnection, entry: CompetitionCatalogEntry, season_id: str) -> None:
    for source, source_entity_id in entry.source.external_ids.items():
        await conn.execute(
            text(
                """
                insert into entity_external_refs
                  (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name, confidence, is_primary, payload)
                values
                  ('COMPETITION_SEASON', cast(:season_id as uuid), :source, 'competition_season', :source_entity_id,
                   :source_entity_name, 1, :is_primary, cast(:payload as jsonb))
                on conflict (entity_type, source, source_entity_id) do update set
                  entity_id = excluded.entity_id,
                  source_entity_name = excluded.source_entity_name,
                  confidence = excluded.confidence,
                  is_primary = excluded.is_primary,
                  payload = entity_external_refs.payload || excluded.payload,
                  updated_at = now()
                """,
            ),
            {
                "season_id": season_id,
                "source": source,
                "source_entity_id": source_entity_id,
                "source_entity_name": entry.name,
                "is_primary": source == entry.source.primary,
                "payload": _json({"catalog_slug": entry.slug, "format_code": entry.format_code}),
            },
        )


async def discover_competition_sources(conn: AsyncConnection, competition: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    entry = get_catalog_entry(competition or settings.default_season_slug)
    await seed_competition_catalog(conn, entry.slug)
    season = await _season_row(conn, entry.slug)
    coverage: dict[str, Any] = {}
    for source in [entry.source.primary, *entry.source.secondary]:
        coverage[source] = await _probe_source(source, entry)
    payload = {
        "competition": entry.slug,
        "coverage": coverage,
        "generated_at": iso_utc(),
    }
    await _merge_season_metadata(conn, season["competition_season_id"], {"source_discovery": payload})
    await _insert_raw_payload(conn, "SOURCE_DISCOVERY", entry.slug, payload)
    return {
        "status": "OK" if any(item.get("available") for item in coverage.values()) else "WARN",
        "job_name": "discover_competition_sources",
        "records_processed": len(coverage),
        "competition": entry.slug,
        "coverage": coverage,
    }


async def sync_competition_fixtures(conn: AsyncConnection, competition: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    entry = get_catalog_entry(competition or settings.default_season_slug)
    await seed_competition_catalog(conn, entry.slug)
    season = await _season_row(conn, entry.slug)
    source = entry.source.primary
    payload = await _fetch_fixture_probe(source, entry)
    records = 0
    if payload:
        await _insert_raw_payload(conn, source, f"{entry.slug}:fixtures", payload)
        records = len(payload.get("matches") or payload.get("fixtures") or payload.get("response") or payload.get("data") or [])
    sync_payload = {
        "competition": entry.slug,
        "source": source,
        "format_code": entry.format_code,
        "records_seen": records,
        "note": "Raw fixture probe stored. Canonical promotion is handled by source-specific adapters.",
        "generated_at": iso_utc(),
    }
    await _merge_season_metadata(conn, season["competition_season_id"], {"last_fixture_sync": sync_payload})
    return {
        "status": "OK" if payload else "WARN",
        "job_name": "sync_competition_fixtures",
        "records_processed": records,
        **sync_payload,
    }


async def worldcup_daily_refresh(conn: AsyncConnection, competition: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    entry = get_catalog_entry(competition or settings.default_season_slug)
    await seed_competition_catalog(conn, entry.slug)
    season = await _season_row(conn, entry.slug)
    dates = [(utc_now().date() + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]
    results: list[dict[str, Any]] = []
    records = 0
    logger.info("worldcup_daily_refresh started competition=%s dates=%s", entry.slug, dates)

    espn_result = await _sync_espn_scoreboard_window(conn, entry, season, dates)
    results.append(espn_result)
    records += int(espn_result.get("records_processed") or 0)
    logger.info("worldcup_daily_refresh ESPN result=%s", espn_result)

    if settings.football_data_token:
        football_data_result = await _sync_football_data_matches(conn, entry, season)
        results.append(football_data_result)
        records += int(football_data_result.get("records_processed") or 0)
        logger.info("worldcup_daily_refresh FootballData result=%s", football_data_result)
    else:
        logger.info("worldcup_daily_refresh FootballData skipped reason=missing_token")

    status = "OK" if any(item.get("status") == "OK" for item in results) else "WARN"
    payload = {
        "competition": entry.slug,
        "window_dates": dates,
        "results": results,
        "generated_at": iso_utc(),
    }
    await _merge_season_metadata(conn, season["competition_season_id"], {"last_daily_refresh": payload})
    logger.info("worldcup_daily_refresh finished status=%s records=%s", status, records)
    return {
        "status": status,
        "job_name": "worldcup_daily_refresh",
        "records_processed": records,
        **payload,
    }


async def worldcup_live_refresh(conn: AsyncConnection, competition: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    entry = get_catalog_entry(competition or settings.default_season_slug)
    await seed_competition_catalog(conn, entry.slug)
    season = await _season_row(conn, entry.slug)
    now = utc_now()
    today = now.date().isoformat()
    # Also fetch yesterday in UTC so late-night matches (e.g. 23:00 Chile = 02:00 UTC next day)
    # are still updated when the live refresh runs past midnight UTC.
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    dates = [yesterday, today] if now.hour < 6 else [today]
    result = await _sync_espn_scoreboard_window(conn, entry, season, dates)
    return {
        "status": result.get("status", "WARN"),
        "job_name": "worldcup_live_refresh",
        "records_processed": int(result.get("records_processed") or 0),
        "date": today,
        "result": result,
        "generated_at": iso_utc(),
    }


async def _season_row(conn: AsyncConnection, slug: str) -> dict[str, Any]:
    result = await conn.execute(
        text("select competition_season_id::text, slug, metadata from competition_seasons where slug = :slug"),
        {"slug": slug},
    )
    row = result.first()
    if not row:
        raise RuntimeError(f"Competition season not found after catalog seed: {slug}")
    return dict(row._mapping)


async def _merge_season_metadata(conn: AsyncConnection, season_id: str, patch: dict[str, Any]) -> None:
    await conn.execute(
        text(
            """
            update competition_seasons
            set metadata = metadata || cast(:patch as jsonb),
                updated_at = now()
            where competition_season_id = cast(:season_id as uuid)
            """,
        ),
        {"season_id": season_id, "patch": _json(patch)},
    )


async def _insert_raw_payload(conn: AsyncConnection, source: str, entity_id: str, payload: dict[str, Any]) -> None:
    await conn.execute(
        text(
            """
            insert into raw_source_payloads
              (source, source_entity_type, source_entity_id, payload_hash, payload, status)
            values
              (:source, :source_entity_type, :source_entity_id, :payload_hash, cast(:payload as jsonb), 'RECEIVED')
            on conflict (source, source_entity_type, payload_hash) do nothing
            """,
        ),
        {
            "source": source,
            "source_entity_type": "competition_discovery" if source == "SOURCE_DISCOVERY" else "fixtures_probe",
            "source_entity_id": entity_id,
            "payload_hash": sha256_json(payload),
            "payload": _json(payload),
        },
    )


async def _sync_espn_scoreboard_window(
    conn: AsyncConnection,
    entry: CompetitionCatalogEntry,
    season: dict[str, Any],
    dates: list[str],
) -> dict[str, Any]:
    updated = 0
    raw_events = 0
    errors: list[dict[str, str]] = []
    client = EspnClient()
    for date in dates:
        try:
            logger.info("ESPN scoreboard request date=%s", date)
            payload = await client.scoreboard(date.replace("-", ""))
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            logger.warning("ESPN scoreboard failed date=%s error=%s", date, type(exc).__name__)
            errors.append({"date": date, "error": type(exc).__name__})
            continue
        await _insert_raw_payload(conn, "ESPN", f"{entry.slug}:scoreboard:{date}", payload)
        events = payload.get("events") or []
        logger.info("ESPN scoreboard response date=%s events=%s", date, len(events))
        if not events:
            today_str = utc_now().date().isoformat()
            severity = "WARN" if date == today_str else "INFO"
            await ObservabilityRepository(conn).data_quality_event(
                "INGESTION",
                severity,
                "EMPTY_ESPN_RESPONSE",
                f"ESPN returned 0 events for date={date} competition={entry.slug}",
                {"date": date, "competition": entry.slug},
            )
        for event in events:
            raw_events += 1
            await _insert_raw_payload(conn, "ESPN", f"event:{event.get('id')}", event)
            if await _promote_normalized_match(conn, season, _normalize_espn_event(event)):
                updated += 1
    logger.info("ESPN scoreboard window finished raw_events=%s updated=%s errors=%s", raw_events, updated, len(errors))
    return {
        "status": "OK" if raw_events else "WARN",
        "source": "ESPN",
        "records_processed": updated,
        "raw_events": raw_events,
        "errors": errors,
    }


async def _sync_football_data_matches(
    conn: AsyncConnection,
    entry: CompetitionCatalogEntry,
    season: dict[str, Any],
) -> dict[str, Any]:
    code = entry.source.external_ids.get("FOOTBALL_DATA")
    if not code:
        return {"status": "WARN", "source": "FOOTBALL_DATA", "records_processed": 0, "reason": "NO_EXTERNAL_ID"}
    try:
        logger.info("FootballData matches request code=%s season=%s", code, entry.season_label[:4])
        payload = await FootballDataClient().competition_matches(code, entry.season_label[:4])
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("FootballData matches failed code=%s error=%s", code, type(exc).__name__)
        return {"status": "WARN", "source": "FOOTBALL_DATA", "records_processed": 0, "error": type(exc).__name__}

    await _insert_raw_payload(conn, "FOOTBALL_DATA", f"{entry.slug}:matches", payload)
    updated = 0
    matches = payload.get("matches") or []
    logger.info("FootballData matches response matches=%s", len(matches))
    for match in matches:
        await _insert_raw_payload(conn, "FOOTBALL_DATA", f"match:{match.get('id')}", match)
        normalized = _normalize_football_data_match(match)
        if normalized.get("kickoff_at") and _date_ymd(normalized["kickoff_at"]) < (utc_now().date() - timedelta(days=2)).isoformat():
            continue
        if await _promote_normalized_match(conn, season, normalized):
            updated += 1
    logger.info("FootballData matches finished raw_matches=%s updated=%s", len(matches), updated)
    return {
        "status": "OK" if matches else "WARN",
        "source": "FOOTBALL_DATA",
        "records_processed": updated,
        "raw_matches": len(matches),
    }


def _normalize_espn_event(event: dict[str, Any]) -> dict[str, Any]:
    competition = (event.get("competitions") or [{}])[0] or {}
    competitors = competition.get("competitors") or []
    home = next((item for item in competitors if str(item.get("homeAway", "")).lower() == "home"), competitors[0] if competitors else {})
    away = next((item for item in competitors if str(item.get("homeAway", "")).lower() == "away"), competitors[1] if len(competitors) > 1 else {})
    status_type = ((competition.get("status") or event.get("status") or {}).get("type")) or {}
    venue = competition.get("venue") or {}
    address = venue.get("address") or {}
    stage_text = (
        ((event.get("seasonType") or {}).get("name"))
        or ((event.get("group") or {}).get("name"))
        or (((competition.get("notes") or [{}])[0] or {}).get("headline"))
        or event.get("name")
        or ""
    )
    group_text = ((event.get("group") or {}).get("name")) or ((event.get("group") or {}).get("shortName")) or stage_text
    return {
        "source": "ESPN",
        "source_match_id": str(event.get("id") or ""),
        "source_match_name": event.get("name") or event.get("shortName") or "",
        "kickoff_at": event.get("date") or competition.get("date"),
        "status": _normalize_espn_status(status_type),
        "stage_code": _normalize_stage_code(stage_text),
        "group_code": _normalize_group_code(group_text),
        "home": _normalize_espn_team(home),
        "away": _normalize_espn_team(away),
        "home_score": _score(home.get("score")),
        "away_score": _score(away.get("score")),
        "venue": {
            "source_venue_id": str(venue.get("id") or ""),
            "display_name": venue.get("fullName") or venue.get("name"),
            "city": address.get("city"),
            "country_code": address.get("country"),
            "timezone_name": venue.get("timeZone"),
            "latitude": venue.get("latitude"),
            "longitude": venue.get("longitude"),
        }
        if venue
        else None,
        "payload": event,
    }


def _normalize_football_data_match(match: dict[str, Any]) -> dict[str, Any]:
    home_team = match.get("homeTeam") or {}
    away_team = match.get("awayTeam") or {}
    score = (match.get("score") or {}).get("fullTime") or {}
    return {
        "source": "FOOTBALL_DATA",
        "source_match_id": str(match.get("id") or ""),
        "source_match_name": f"{home_team.get('name', '')} vs {away_team.get('name', '')}",
        "kickoff_at": match.get("utcDate"),
        "status": _normalize_football_data_status(match.get("status")),
        "stage_code": _normalize_stage_code(match.get("stage") or match.get("group")),
        "group_code": _normalize_group_code(match.get("group")),
        "home": _normalize_football_data_team(home_team),
        "away": _normalize_football_data_team(away_team),
        "home_score": score.get("home"),
        "away_score": score.get("away"),
        "venue": None,
        "payload": match,
    }


async def _promote_normalized_match(conn: AsyncConnection, season: dict[str, Any], normalized: dict[str, Any]) -> bool:
    if not normalized.get("kickoff_at") or not normalized.get("home") or not normalized.get("away"):
        return False
    match = await _find_existing_match(conn, season, normalized)
    if not match:
        logger.info(
            "match promotion skipped source=%s source_match_id=%s name=%s reason=no_existing_match",
            normalized.get("source"),
            normalized.get("source_match_id"),
            normalized.get("source_match_name"),
        )
        await ObservabilityRepository(conn).data_quality_event(
            "STAGING",
            "WARN",
            "MATCH_NOT_FOUND",
            f"No canonical match found for {normalized.get('source')}:{normalized.get('source_match_id')} — {normalized.get('source_match_name')}",
            {
                "source": normalized.get("source"),
                "source_match_id": normalized.get("source_match_id"),
                "source_match_name": normalized.get("source_match_name"),
                "season": season.get("slug"),
                "kickoff_at": str(normalized.get("kickoff_at") or ""),
            },
        )
        return False
    home_score = _score(normalized.get("home_score"))
    away_score = _score(normalized.get("away_score"))
    home_team_id = match.get("home_team_id")
    away_team_id = match.get("away_team_id")
    winner_team_id = _winner_team_id(home_team_id, away_team_id, home_score, away_score, normalized.get("status"))
    await conn.execute(
        text(
            """
            update matches
            set kickoff_at = cast(:kickoff_at as timestamptz),
                status = cast(:status as match_status),
                home_score = :home_score,
                away_score = :away_score,
                winner_team_id = cast(:winner_team_id as uuid),
                metadata = metadata || cast(:metadata as jsonb),
                updated_at = now()
            where match_id = cast(:match_id as uuid)
            """
        ),
        {
            "match_id": match["match_id"],
            "kickoff_at": _parse_iso_dt(normalized["kickoff_at"]),
            "status": normalized["status"],
            "home_score": home_score,
            "away_score": away_score,
            "winner_team_id": winner_team_id,
            "metadata": _json({"source": normalized["source"], "source_match_name": normalized.get("source_match_name")}),
        },
    )
    await _update_participant_score(conn, match["match_id"], "HOME", home_score)
    await _update_participant_score(conn, match["match_id"], "AWAY", away_score)
    await _upsert_match_external_ref(conn, match["match_id"], normalized)
    logger.info(
        "match promoted match_id=%s source=%s source_match_id=%s status=%s score=%s-%s",
        match["match_id"],
        normalized.get("source"),
        normalized.get("source_match_id"),
        normalized.get("status"),
        home_score,
        away_score,
    )
    return True


async def _find_existing_match(conn: AsyncConnection, season: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any] | None:
    source_match_id = normalized.get("source_match_id")
    if source_match_id:
        row = await conn.execute(
            text(
                """
                select m.match_id::text, home.team_id::text as home_team_id, away.team_id::text as away_team_id
                from entity_external_refs ref
                join matches m on m.match_id = ref.entity_id
                left join match_participants home on home.match_id = m.match_id and home.side = 'HOME'
                left join match_participants away on away.match_id = m.match_id and away.side = 'AWAY'
                where ref.entity_type = 'MATCH'
                  and ref.source = :source
                  and ref.source_entity_id = :source_match_id
                limit 1
                """
            ),
            {"source": normalized["source"], "source_match_id": source_match_id},
        )
        found = row.first()
        if found:
            return dict(found._mapping)

    slug = _match_slug(season["slug"], normalized)
    row = await conn.execute(
        text(
            """
            select m.match_id::text, home.team_id::text as home_team_id, away.team_id::text as away_team_id
            from matches m
            left join match_participants home on home.match_id = m.match_id and home.side = 'HOME'
            left join match_participants away on away.match_id = m.match_id and away.side = 'AWAY'
            where m.competition_season_id = cast(:season_id as uuid)
              and m.slug = :slug
            limit 1
            """
        ),
        {"season_id": season["competition_season_id"], "slug": slug},
    )
    found = row.first()
    if found:
        return dict(found._mapping)

    candidates = await conn.execute(
        text(
            """
            select
              m.match_id::text,
              home.team_id::text as home_team_id,
              away.team_id::text as away_team_id,
              home_team.display_name as home_name,
              away_team.display_name as away_name
            from matches m
            join match_participants home on home.match_id = m.match_id and home.side = 'HOME'
            join match_participants away on away.match_id = m.match_id and away.side = 'AWAY'
            join teams home_team on home_team.team_id = home.team_id
            join teams away_team on away_team.team_id = away.team_id
            where m.competition_season_id = cast(:season_id as uuid)
              and date(m.kickoff_at at time zone 'UTC') = cast(:kickoff_date as date)
            """
        ),
        {"season_id": season["competition_season_id"], "kickoff_date": _parse_iso_dt(normalized["kickoff_at"]).date()},
    )
    wanted_home = _normalize_name((normalized.get("home") or {}).get("display_name"))
    wanted_away = _normalize_name((normalized.get("away") or {}).get("display_name"))
    for candidate in candidates:
        data = dict(candidate._mapping)
        if _normalize_name(data.get("home_name")) == wanted_home and _normalize_name(data.get("away_name")) == wanted_away:
            return data
    return None


async def _update_participant_score(conn: AsyncConnection, match_id: str, side: str, score: int | None) -> None:
    await conn.execute(
        text(
            """
            update match_participants
            set score = :score,
                updated_at = now()
            where match_id = cast(:match_id as uuid)
              and side = cast(:side as participant_side)
            """
        ),
        {"match_id": match_id, "side": side, "score": score},
    )


async def _upsert_match_external_ref(conn: AsyncConnection, match_id: str, normalized: dict[str, Any]) -> None:
    if not normalized.get("source_match_id"):
        return
    await conn.execute(
        text(
            """
            insert into entity_external_refs
              (entity_type, entity_id, source, source_entity_type, source_entity_id, source_entity_name, confidence, is_primary, payload)
            values
              ('MATCH', cast(:match_id as uuid), :source, 'event', :source_entity_id, :source_entity_name, 1, true, cast(:payload as jsonb))
            on conflict (entity_type, source, source_entity_id) do update set
              entity_id = excluded.entity_id,
              source_entity_name = excluded.source_entity_name,
              confidence = excluded.confidence,
              is_primary = excluded.is_primary,
              payload = excluded.payload,
              updated_at = now()
            """
        ),
        {
            "match_id": match_id,
            "source": normalized["source"],
            "source_entity_id": normalized["source_match_id"],
            "source_entity_name": normalized.get("source_match_name") or "",
            "payload": _json(normalized.get("payload") or {}),
        },
    )


def _normalize_espn_team(competitor: dict[str, Any]) -> dict[str, Any]:
    team = competitor.get("team") or competitor
    name = team.get("displayName") or team.get("name") or competitor.get("displayName") or competitor.get("name") or ""
    return {"display_name": name, "source_team_id": str(team.get("id") or competitor.get("id") or ""), "payload": competitor}


def _normalize_football_data_team(team: dict[str, Any]) -> dict[str, Any]:
    return {"display_name": team.get("name") or team.get("shortName") or team.get("tla") or "", "source_team_id": str(team.get("id") or ""), "payload": team}


def _normalize_espn_status(status_type: dict[str, Any]) -> str:
    name = str(status_type.get("name") or status_type.get("state") or status_type.get("description") or "").upper()
    if status_type.get("completed") or "FINAL" in name or name == "STATUS_FINAL":
        return "FINISHED"
    if "IN_PROGRESS" in name or "HALFTIME" in name:
        return "LIVE"
    if "POSTPONED" in name:
        return "POSTPONED"
    if "CANCELED" in name or "CANCELLED" in name:
        return "CANCELLED"
    return "SCHEDULED"


def _normalize_football_data_status(status: Any) -> str:
    value = str(status or "").upper()
    if value == "FINISHED":
        return "FINISHED"
    if value in {"LIVE", "IN_PLAY", "PAUSED"}:
        return "LIVE"
    if value == "POSTPONED":
        return "POSTPONED"
    if value in {"CANCELLED", "CANCELED", "SUSPENDED"}:
        return "CANCELLED"
    return "SCHEDULED"


def _normalize_stage_code(value: Any) -> str:
    raw = _normalize_name(value)
    if "round of 32" in raw or "last 32" in raw or "dieciseis" in raw:
        return "ROUND_OF_32"
    if "round of 16" in raw or "last 16" in raw or "octav" in raw:
        return "ROUND_OF_16"
    if "quarter" in raw or "cuarto" in raw:
        return "QUARTER_FINAL"
    if "semi" in raw:
        return "SEMI_FINAL"
    if "third" in raw or "tercer" in raw:
        return "THIRD_PLACE"
    if raw == "final" or " final" in raw:
        return "FINAL"
    return "GROUP_STAGE"


def _normalize_group_code(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"(?:group|grupo)\s*([a-l])", raw, flags=re.IGNORECASE) or re.match(r"^([A-L])$", raw, flags=re.IGNORECASE)
    return f"Grupo {match.group(1).upper()}" if match else None


def _score(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _winner_team_id(home_team_id: str | None, away_team_id: str | None, home_score: int | None, away_score: int | None, status: str | None) -> str | None:
    if status != "FINISHED" or home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return home_team_id
    if away_score > home_score:
        return away_team_id
    return None


def _match_slug(season_slug: str, normalized: dict[str, Any]) -> str:
    home = (normalized.get("home") or {}).get("display_name")
    away = (normalized.get("away") or {}).get("display_name")
    return _slug(f"{season_slug} {_date_ymd(normalized.get('kickoff_at'))} {home} {away}")


async def _probe_source(source: str, entry: CompetitionCatalogEntry) -> dict[str, Any]:
    try:
        if source == "SPORTMONKS":
            if not get_settings().sportmonks_api_token:
                return _declared_source(source, entry, available=False, reason="missing_token")
            payload = await SportmonksClient().leagues(per_page=1)
            return _source_result(source, entry, payload)
        if source == "FOOTBALL_DATA":
            if not get_settings().football_data_token:
                return _declared_source(source, entry, available=False, reason="missing_token")
            code = entry.source.external_ids.get("FOOTBALL_DATA")
            payload = await FootballDataClient().competition(code) if code else await FootballDataClient().competitions()
            return _source_result(source, entry, payload)
        if source == "API_FOOTBALL":
            if not get_settings().api_football_key:
                return _declared_source(source, entry, available=False, reason="missing_token")
            payload = await ApiFootballClient().leagues(search=entry.name)
            return _source_result(source, entry, payload)
        if source == "ESPN":
            payload = await EspnClient().scoreboard()
            return _source_result(source, entry, payload)
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        return _declared_source(source, entry, available=False, reason=type(exc).__name__, error=str(exc))
    return _declared_source(source, entry, available=False, reason="unsupported_source")


async def _fetch_fixture_probe(source: str, entry: CompetitionCatalogEntry) -> dict[str, Any] | None:
    try:
        if source == "FOOTBALL_DATA" and get_settings().football_data_token:
            code = entry.source.external_ids.get("FOOTBALL_DATA")
            if code:
                return await FootballDataClient().competition_matches(code)
        if source == "API_FOOTBALL" and get_settings().api_football_key:
            league = entry.source.external_ids.get("API_FOOTBALL")
            return await ApiFootballClient().fixtures(league=league, season=entry.season_label[:4] if league else None, next_count=10)
        if source == "SPORTMONKS" and get_settings().sportmonks_api_token:
            return await SportmonksClient().fixtures(page=1, per_page=25)
        if source == "ESPN":
            return await EspnClient().scoreboard()
    except (httpx.HTTPError, TimeoutError, OSError):
        return None
    return None


def _declared_source(
    source: str,
    entry: CompetitionCatalogEntry,
    available: bool,
    reason: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "available": available,
        "reason": reason,
        "error": error,
        "capabilities": entry.source.capabilities.get(source, []),
        "external_id": entry.source.external_ids.get(source),
    }


def _source_result(source: str, entry: CompetitionCatalogEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return _declared_source(source, entry, available=True) | {
        "payload_keys": sorted(payload.keys()),
        "sample_size": len(payload.get("data") or payload.get("response") or payload.get("matches") or []),
    }

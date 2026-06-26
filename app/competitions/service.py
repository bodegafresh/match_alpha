from __future__ import annotations

import json
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
from app.core.time import iso_utc
from app.normalization.competition_format import get_format_normalizer


def _json(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


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
            "starts_at": entry.starts_at,
            "ends_at": entry.ends_at,
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


async def _season_row(conn: AsyncConnection, slug: str) -> dict[str, Any]:
    result = await conn.execute(
        text("select competition_season_id::text, metadata from competition_seasons where slug = :slug"),
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

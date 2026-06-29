from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.clients.api_football_client import ApiFootballClient
from app.clients.sportmonks_client import SportmonksClient
from app.core.config import get_settings
from app.core.time import iso_utc, utc_now
from app.normalization.player_identity import normalize_identity_name
from app.normalization.team_normalizer import slugify_name


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _normalize_stat_name(value: str) -> str:
    raw = normalize_identity_name(value)
    return raw.replace(" ", "_") if raw else "unknown"


def _to_numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)

    text_value = str(value).strip()
    if not text_value:
        return None

    if text_value.endswith("%"):
        text_value = text_value[:-1].strip()

    text_value = text_value.replace(",", ".")
    try:
        return float(text_value)
    except ValueError:
        return None


def _flatten_player_statistics(node: Any, prefix: str = "") -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            nested_prefix = f"{prefix}_{_normalize_stat_name(str(key))}" if prefix else _normalize_stat_name(str(key))
            out.update(_flatten_player_statistics(value, nested_prefix))
        return out
    if isinstance(node, list):
        for item in node:
            out.update(_flatten_player_statistics(item, prefix))
        return out

    if not prefix:
        return out

    out[prefix] = _to_numeric(node)
    return out


async def sync_finished_match_stats(conn: AsyncConnection, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    settings = get_settings()

    days_back = int(payload.get("days_back", 1) or 1)
    target_date = (utc_now().date() - timedelta(days=days_back)).isoformat()

    matches_result = await conn.execute(
        text(
            """
            SELECT
              m.match_id::text,
              m.status,
              m.kickoff_at,
              date(m.kickoff_at at time zone 'UTC')::text as kickoff_date
            FROM matches m
            JOIN competition_seasons cs ON cs.competition_season_id = m.competition_season_id
            WHERE m.status = 'FINISHED'
              AND date(m.kickoff_at at time zone 'UTC') = cast(:target_date as date)
              AND cs.status in ('ACTIVE', 'SCHEDULED')
            """
        ),
        {"target_date": target_date},
    )
    matches = [dict(r._mapping) for r in matches_result]

    api_football_client = ApiFootballClient() if settings.api_football_key else None
    sportmonks_client = SportmonksClient() if settings.sportmonks_api_token else None

    sportmonks_fixture_cache: dict[str, dict[str, Any]] = {}
    if sportmonks_client:
        try:
            sportmonks_payload = await sportmonks_client.fixtures(
                date_from=target_date,
                date_to=target_date,
                include="participants;lineups;lineups.player;statistics;events",
                page=1,
                per_page=100,
            )
            for fixture in sportmonks_payload.get("data") or []:
                fixture_id = str(fixture.get("id") or "")
                if fixture_id:
                    sportmonks_fixture_cache[fixture_id] = fixture
        except Exception:
            sportmonks_fixture_cache = {}

    processed = 0
    events_written = 0
    lineups_written = 0
    player_stats_written = 0
    team_stats_updated = 0
    unresolved_players = 0
    source_usage: dict[str, int] = {"API_FOOTBALL": 0, "SPORTMONKS": 0}
    errors: list[str] = []

    for match in matches:
        match_id = str(match["match_id"])
        refs_result = await conn.execute(
            text(
                """
                SELECT source, source_entity_id
                FROM entity_external_refs
                WHERE entity_type = 'MATCH'
                  AND entity_id = cast(:match_id as uuid)
                  AND source IN ('API_FOOTBALL', 'SPORTMONKS')
                                ORDER BY CASE source WHEN 'SPORTMONKS' THEN 0 ELSE 1 END
                """
            ),
            {"match_id": match_id},
        )
        refs = [dict(r._mapping) for r in refs_result]

        used_source: str | None = None
        for ref in refs:
            source = str(ref.get("source") or "")
            source_match_id = str(ref.get("source_entity_id") or "")
            if not source_match_id:
                continue

            try:
                if source == "API_FOOTBALL" and api_football_client:
                    source_events = await api_football_client.fixture_events(source_match_id)
                    source_lineups = await api_football_client.fixture_lineups(source_match_id)
                    source_players = await api_football_client.fixture_players(source_match_id)
                    source_team_stats = await api_football_client.fixture_statistics(source_match_id)

                    events_written += await _upsert_api_football_events(conn, match_id, source_match_id, source_events)
                    lineups_result = await _upsert_api_football_lineups(conn, match_id, source_lineups)
                    lineups_written += lineups_result["lineups_written"]
                    unresolved_players += lineups_result["unresolved_players"]

                    players_result = await _upsert_api_football_player_stats(conn, match_id, source_players)
                    player_stats_written += players_result["player_stats_written"]
                    unresolved_players += players_result["unresolved_players"]

                    team_stats_updated += await _upsert_api_football_team_stats(conn, match_id, source_team_stats)
                    used_source = source
                    break

                if source == "SPORTMONKS" and source_match_id in sportmonks_fixture_cache:
                    fixture = sportmonks_fixture_cache[source_match_id]
                    events_written += await _upsert_sportmonks_events(conn, match_id, source_match_id, fixture)
                    lineups_result = await _upsert_sportmonks_lineups(conn, match_id, fixture)
                    lineups_written += lineups_result["lineups_written"]
                    unresolved_players += lineups_result["unresolved_players"]
                    team_stats_updated += await _upsert_sportmonks_team_stats(conn, match_id, fixture)
                    used_source = source
                    break
            except Exception as exc:
                errors.append(f"{match_id}/{source}: {type(exc).__name__}:{exc}")

        if used_source:
            source_usage[used_source] += 1
            processed += 1
            await conn.execute(
                text(
                    """
                    UPDATE matches
                    SET metadata = metadata || cast(:metadata as jsonb),
                        updated_at = now()
                    WHERE match_id = cast(:match_id as uuid)
                    """
                ),
                {
                    "match_id": match_id,
                    "metadata": _json(
                        {
                            "finished_stats": {
                                "last_synced_at": iso_utc(),
                                "source": used_source,
                                "target_date": target_date,
                            }
                        }
                    ),
                },
            )

    status = "WARN" if errors else "OK"
    return {
        "status": status,
        "job_name": "finished_match_stats_refresh",
        "records_processed": processed,
        "target_date": target_date,
        "matches_finished": len(matches),
        "matches_enriched": processed,
        "source_usage": source_usage,
        "events_written": events_written,
        "lineups_written": lineups_written,
        "player_stats_written": player_stats_written,
        "team_stats_updated": team_stats_updated,
        "unresolved_players": unresolved_players,
        "errors": errors[:50],
        "generated_at": iso_utc(),
    }


async def _resolve_team_id_for_source(conn: AsyncConnection, source: str, source_team_id: str) -> str | None:
    row = await conn.execute(
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
    return row.scalar_one_or_none()


async def _resolve_or_create_player_for_source(
    conn: AsyncConnection,
    *,
    source: str,
    source_player_id: str,
    display_name: str,
) -> str | None:
    if not source_player_id and not display_name:
        return None

    if source_player_id:
        row = await conn.execute(
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
        player_id = row.scalar_one_or_none()
        if player_id:
            return player_id

    normalized_name = normalize_identity_name(display_name)
    if normalized_name:
        row = await conn.execute(
            text(
                """
                SELECT player_id::text
                FROM players
                WHERE normalized_name = :normalized_name
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"normalized_name": normalized_name},
        )
        player_id = row.scalar_one_or_none()
        if player_id:
            if source_player_id:
                await _upsert_player_external_ref(conn, player_id, source, source_player_id, display_name)
            return player_id

    if not source_player_id or not display_name:
        return None

    slug = f"{slugify_name(display_name)}-{source.lower()}-{source_player_id}"[:180]
    created = await conn.execute(
        text(
            """
            INSERT INTO players
              (slug, display_name, normalized_name, metadata)
            VALUES
              (:slug, :display_name, :normalized_name, cast(:metadata as jsonb))
            RETURNING player_id::text
            """
        ),
        {
            "slug": slug,
            "display_name": display_name,
            "normalized_name": normalized_name or display_name.lower(),
            "metadata": _json({"source": source, "created_by": "finished_match_stats_refresh"}),
        },
    )
    player_id = created.scalar_one()
    await _upsert_player_external_ref(conn, player_id, source, source_player_id, display_name)
    await _upsert_player_alias(conn, player_id, display_name, source)
    return player_id


async def _upsert_player_external_ref(conn: AsyncConnection, player_id: str, source: str, source_player_id: str, source_player_name: str) -> None:
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
            "payload": _json({"resolver": "finished_match_stats_refresh", "source": source}),
        },
    )


async def _upsert_player_alias(conn: AsyncConnection, player_id: str, alias: str, source: str) -> None:
    normalized_alias = normalize_identity_name(alias)
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


async def _upsert_api_football_events(conn: AsyncConnection, match_id: str, fixture_id: str, payload: dict[str, Any]) -> int:
    written = 0
    for idx, event in enumerate(payload.get("response") or []):
        team_id = await _resolve_team_id_for_source(
            conn,
            "API_FOOTBALL",
            str((event.get("team") or {}).get("id") or ""),
        )

        player_info = event.get("player") or {}
        assist_info = event.get("assist") or {}
        player_id = await _resolve_or_create_player_for_source(
            conn,
            source="API_FOOTBALL",
            source_player_id=str(player_info.get("id") or ""),
            display_name=str(player_info.get("name") or "").strip(),
        )
        related_player_id = await _resolve_or_create_player_for_source(
            conn,
            source="API_FOOTBALL",
            source_player_id=str(assist_info.get("id") or ""),
            display_name=str(assist_info.get("name") or "").strip(),
        )

        elapsed = (event.get("time") or {}).get("elapsed")
        extra = (event.get("time") or {}).get("extra")
        source_event_id = f"{fixture_id}:{idx}:{elapsed}:{extra}:{event.get('type')}:{event.get('detail')}"

        await conn.execute(
            text(
                """
                INSERT INTO match_events
                  (match_id, team_id, player_id, related_player_id, event_type, event_detail,
                   minute, stoppage_minute, source, source_event_id, payload)
                VALUES
                  (cast(:match_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid), cast(:related_player_id as uuid),
                   :event_type, :event_detail, :minute, :stoppage_minute, :source, :source_event_id, cast(:payload as jsonb))
                ON CONFLICT (source, source_event_id) DO UPDATE SET
                  team_id = COALESCE(excluded.team_id, match_events.team_id),
                  player_id = COALESCE(excluded.player_id, match_events.player_id),
                  related_player_id = COALESCE(excluded.related_player_id, match_events.related_player_id),
                  event_type = excluded.event_type,
                  event_detail = excluded.event_detail,
                  minute = COALESCE(excluded.minute, match_events.minute),
                  stoppage_minute = COALESCE(excluded.stoppage_minute, match_events.stoppage_minute),
                  payload = match_events.payload || excluded.payload
                """
            ),
            {
                "match_id": match_id,
                "team_id": team_id,
                "player_id": player_id,
                "related_player_id": related_player_id,
                "event_type": str(event.get("type") or "UNKNOWN"),
                "event_detail": str(event.get("detail") or event.get("comments") or ""),
                "minute": int(elapsed) if elapsed is not None else None,
                "stoppage_minute": int(extra) if extra is not None else None,
                "source": "API_FOOTBALL",
                "source_event_id": source_event_id,
                "payload": _json(event),
            },
        )
        written += 1
    return written


async def _upsert_api_football_lineups(conn: AsyncConnection, match_id: str, payload: dict[str, Any]) -> dict[str, int]:
    lineups_written = 0
    unresolved_players = 0

    for team_node in payload.get("response") or []:
        team_id = await _resolve_team_id_for_source(
            conn,
            "API_FOOTBALL",
            str((team_node.get("team") or {}).get("id") or ""),
        )
        if not team_id:
            continue

        lineup_groups = [
            (team_node.get("startXI") or [], "STARTER"),
            (team_node.get("substitutes") or [], "SUBSTITUTE"),
        ]

        for players_group, lineup_role in lineup_groups:
            for item in players_group:
                player_info = item.get("player") or {}
                player_id = await _resolve_or_create_player_for_source(
                    conn,
                    source="API_FOOTBALL",
                    source_player_id=str(player_info.get("id") or ""),
                    display_name=str(player_info.get("name") or "").strip(),
                )
                if not player_id:
                    unresolved_players += 1
                    continue

                await conn.execute(
                    text(
                        """
                        INSERT INTO match_lineups
                          (match_id, team_id, player_id, lineup_role, position, shirt_number, is_captain, source, metadata)
                        VALUES
                          (cast(:match_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid),
                           cast(:lineup_role as lineup_role), :position, :shirt_number, :is_captain, :source, cast(:metadata as jsonb))
                        ON CONFLICT (match_id, team_id, player_id, source) DO UPDATE SET
                          lineup_role = COALESCE(excluded.lineup_role, match_lineups.lineup_role),
                          position = COALESCE(excluded.position, match_lineups.position),
                          shirt_number = COALESCE(excluded.shirt_number, match_lineups.shirt_number),
                          is_captain = match_lineups.is_captain OR excluded.is_captain,
                          metadata = match_lineups.metadata || excluded.metadata,
                          updated_at = now()
                        """
                    ),
                    {
                        "match_id": match_id,
                        "team_id": team_id,
                        "player_id": player_id,
                        "lineup_role": lineup_role,
                        "position": player_info.get("pos"),
                        "shirt_number": player_info.get("number"),
                        "is_captain": bool(player_info.get("captain")),
                        "source": "API_FOOTBALL",
                        "metadata": _json({"source": "API_FOOTBALL", "raw": item}),
                    },
                )
                lineups_written += 1

    return {
        "lineups_written": lineups_written,
        "unresolved_players": unresolved_players,
    }


async def _upsert_api_football_player_stats(conn: AsyncConnection, match_id: str, payload: dict[str, Any]) -> dict[str, int]:
    player_stats_written = 0
    unresolved_players = 0

    for team_bucket in payload.get("response") or []:
        team_id = await _resolve_team_id_for_source(
            conn,
            "API_FOOTBALL",
            str((team_bucket.get("team") or {}).get("id") or ""),
        )
        if not team_id:
            continue

        for player_node in team_bucket.get("players") or []:
            player_info = player_node.get("player") or {}
            player_id = await _resolve_or_create_player_for_source(
                conn,
                source="API_FOOTBALL",
                source_player_id=str(player_info.get("id") or ""),
                display_name=str(player_info.get("name") or "").strip(),
            )
            if not player_id:
                unresolved_players += 1
                continue

            flat_stats: dict[str, float | None] = {}
            for stat_block in player_node.get("statistics") or []:
                flat_stats.update(_flatten_player_statistics(stat_block))

            for stat_name, stat_value in flat_stats.items():
                if stat_value is None:
                    continue
                await conn.execute(
                    text(
                        """
                        INSERT INTO player_match_stats
                          (match_id, team_id, player_id, stat_name, stat_value, source, captured_at, payload)
                        VALUES
                          (cast(:match_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid),
                           :stat_name, cast(:stat_value as numeric), :source, now(), cast(:payload as jsonb))
                        ON CONFLICT (match_id, player_id, stat_name, source) DO UPDATE SET
                          team_id = excluded.team_id,
                          stat_value = excluded.stat_value,
                          captured_at = now(),
                          payload = player_match_stats.payload || excluded.payload
                        """
                    ),
                    {
                        "match_id": match_id,
                        "team_id": team_id,
                        "player_id": player_id,
                        "stat_name": stat_name,
                        "stat_value": stat_value,
                        "source": "API_FOOTBALL",
                        "payload": _json({"source": "API_FOOTBALL", "raw": player_node}),
                    },
                )
                player_stats_written += 1

    return {
        "player_stats_written": player_stats_written,
        "unresolved_players": unresolved_players,
    }


async def _upsert_api_football_team_stats(conn: AsyncConnection, match_id: str, payload: dict[str, Any]) -> int:
    updated = 0
    for team_bucket in payload.get("response") or []:
        team_id = await _resolve_team_id_for_source(
            conn,
            "API_FOOTBALL",
            str((team_bucket.get("team") or {}).get("id") or ""),
        )
        if not team_id:
            continue

        stats_map: dict[str, float | str | None] = {}
        for item in team_bucket.get("statistics") or []:
            stat_name = _normalize_stat_name(str(item.get("type") or "unknown"))
            numeric_value = _to_numeric(item.get("value"))
            stats_map[stat_name] = numeric_value if numeric_value is not None else item.get("value")

        await conn.execute(
            text(
                """
                UPDATE match_participants
                SET metadata = metadata || cast(:metadata as jsonb),
                    updated_at = now()
                WHERE match_id = cast(:match_id as uuid)
                  AND team_id = cast(:team_id as uuid)
                """
            ),
            {
                "match_id": match_id,
                "team_id": team_id,
                "metadata": _json(
                    {
                        "team_stats": {
                            "source": "API_FOOTBALL",
                            "values": stats_map,
                        }
                    }
                ),
            },
        )
        updated += 1
    return updated


async def _upsert_sportmonks_events(conn: AsyncConnection, match_id: str, fixture_id: str, fixture: dict[str, Any]) -> int:
    events_node = fixture.get("events")
    events: list[dict[str, Any]] = []
    if isinstance(events_node, list):
        events = [e for e in events_node if isinstance(e, dict)]
    elif isinstance(events_node, dict):
        data = events_node.get("data")
        if isinstance(data, list):
            events = [e for e in data if isinstance(e, dict)]

    written = 0
    for idx, event in enumerate(events):
        team_id = await _resolve_team_id_for_source(conn, "SPORTMONKS", str(event.get("participant_id") or ""))
        player_id = await _resolve_or_create_player_for_source(
            conn,
            source="SPORTMONKS",
            source_player_id=str(event.get("player_id") or ""),
            display_name=str(event.get("player_name") or "").strip(),
        )
        related_player_id = await _resolve_or_create_player_for_source(
            conn,
            source="SPORTMONKS",
            source_player_id=str(event.get("related_player_id") or ""),
            display_name=str(event.get("related_player_name") or "").strip(),
        )

        source_event_id = f"{fixture_id}:{idx}:{event.get('type_id')}:{event.get('minute')}:{event.get('participant_id')}"
        await conn.execute(
            text(
                """
                INSERT INTO match_events
                  (match_id, team_id, player_id, related_player_id, event_type, event_detail,
                   minute, stoppage_minute, source, source_event_id, payload)
                VALUES
                  (cast(:match_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid), cast(:related_player_id as uuid),
                   :event_type, :event_detail, :minute, :stoppage_minute, :source, :source_event_id, cast(:payload as jsonb))
                ON CONFLICT (source, source_event_id) DO UPDATE SET
                  team_id = COALESCE(excluded.team_id, match_events.team_id),
                  player_id = COALESCE(excluded.player_id, match_events.player_id),
                  related_player_id = COALESCE(excluded.related_player_id, match_events.related_player_id),
                  event_type = excluded.event_type,
                  event_detail = excluded.event_detail,
                  minute = COALESCE(excluded.minute, match_events.minute),
                  stoppage_minute = COALESCE(excluded.stoppage_minute, match_events.stoppage_minute),
                  payload = match_events.payload || excluded.payload
                """
            ),
            {
                "match_id": match_id,
                "team_id": team_id,
                "player_id": player_id,
                "related_player_id": related_player_id,
                "event_type": str(event.get("type") or event.get("name") or "UNKNOWN"),
                "event_detail": str(event.get("detail") or event.get("result") or ""),
                "minute": int(event.get("minute")) if event.get("minute") is not None else None,
                "stoppage_minute": int(event.get("extra_minute")) if event.get("extra_minute") is not None else None,
                "source": "SPORTMONKS",
                "source_event_id": source_event_id,
                "payload": _json(event),
            },
        )
        written += 1
    return written


async def _upsert_sportmonks_lineups(conn: AsyncConnection, match_id: str, fixture: dict[str, Any]) -> dict[str, int]:
    lineups_node = fixture.get("lineups")
    entries: list[dict[str, Any]] = []
    if isinstance(lineups_node, list):
        entries = [e for e in lineups_node if isinstance(e, dict)]
    elif isinstance(lineups_node, dict):
        data = lineups_node.get("data")
        if isinstance(data, list):
            entries = [e for e in data if isinstance(e, dict)]

    lineups_written = 0
    unresolved_players = 0
    for item in entries:
        team_id = await _resolve_team_id_for_source(conn, "SPORTMONKS", str(item.get("participant_id") or item.get("team_id") or ""))
        if not team_id:
            continue

        player_node = item.get("player") if isinstance(item.get("player"), dict) else {}
        player_id = await _resolve_or_create_player_for_source(
            conn,
            source="SPORTMONKS",
            source_player_id=str(player_node.get("id") or item.get("player_id") or ""),
            display_name=str(player_node.get("name") or item.get("player_name") or "").strip(),
        )
        if not player_id:
            unresolved_players += 1
            continue

        lineup_type = str(item.get("type") or "").lower()
        lineup_role = "STARTER" if "start" in lineup_type else "SUBSTITUTE"
        await conn.execute(
            text(
                """
                INSERT INTO match_lineups
                  (match_id, team_id, player_id, lineup_role, position, shirt_number, is_captain, source, metadata)
                VALUES
                  (cast(:match_id as uuid), cast(:team_id as uuid), cast(:player_id as uuid),
                   cast(:lineup_role as lineup_role), :position, :shirt_number, :is_captain, :source, cast(:metadata as jsonb))
                ON CONFLICT (match_id, team_id, player_id, source) DO UPDATE SET
                  lineup_role = COALESCE(excluded.lineup_role, match_lineups.lineup_role),
                  position = COALESCE(excluded.position, match_lineups.position),
                  shirt_number = COALESCE(excluded.shirt_number, match_lineups.shirt_number),
                  is_captain = match_lineups.is_captain OR excluded.is_captain,
                  metadata = match_lineups.metadata || excluded.metadata,
                  updated_at = now()
                """
            ),
            {
                "match_id": match_id,
                "team_id": team_id,
                "player_id": player_id,
                "lineup_role": lineup_role,
                "position": player_node.get("position") or item.get("position_name") or item.get("position"),
                "shirt_number": player_node.get("number") or item.get("jersey_number"),
                "is_captain": bool(player_node.get("captain") or item.get("captain")),
                "source": "SPORTMONKS",
                "metadata": _json({"source": "SPORTMONKS", "raw": item}),
            },
        )
        lineups_written += 1

    return {
        "lineups_written": lineups_written,
        "unresolved_players": unresolved_players,
    }


async def _upsert_sportmonks_team_stats(conn: AsyncConnection, match_id: str, fixture: dict[str, Any]) -> int:
    stats_node = fixture.get("statistics")
    stats_entries: list[dict[str, Any]] = []
    if isinstance(stats_node, list):
        stats_entries = [e for e in stats_node if isinstance(e, dict)]
    elif isinstance(stats_node, dict):
        data = stats_node.get("data")
        if isinstance(data, list):
            stats_entries = [e for e in data if isinstance(e, dict)]

    buckets: dict[str, dict[str, Any]] = {}
    for entry in stats_entries:
        participant_id = str(entry.get("participant_id") or "")
        if not participant_id:
            continue
        buckets.setdefault(participant_id, {})
        stat_name = _normalize_stat_name(str(entry.get("type") or entry.get("name") or entry.get("type_id") or "unknown"))
        stat_value = _to_numeric(entry.get("data") if "data" in entry else entry.get("value"))
        buckets[participant_id][stat_name] = stat_value if stat_value is not None else entry.get("value")

    updated = 0
    for participant_id, values in buckets.items():
        team_id = await _resolve_team_id_for_source(conn, "SPORTMONKS", participant_id)
        if not team_id:
            continue
        await conn.execute(
            text(
                """
                UPDATE match_participants
                SET metadata = metadata || cast(:metadata as jsonb),
                    updated_at = now()
                WHERE match_id = cast(:match_id as uuid)
                  AND team_id = cast(:team_id as uuid)
                """
            ),
            {
                "match_id": match_id,
                "team_id": team_id,
                "metadata": _json(
                    {
                        "team_stats": {
                            "source": "SPORTMONKS",
                            "values": values,
                        }
                    }
                ),
            },
        )
        updated += 1
    return updated

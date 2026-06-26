from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.schemas.competition import CompetitionLayoutEnvelope
from app.competitions.catalog import supported_competitions
from app.db.session import get_connection

router = APIRouter(prefix="/competitions", tags=["competitions"])


STAGE_LABELS = {
    "GROUP_STAGE": "Fase de grupos",
    "LEAGUE_PHASE": "Fase liga",
    "PLAYOFF": "Playoff",
    "ROUND_OF_32": "Dieciseisavos",
    "ROUND_OF_16": "Octavos",
    "QUARTER_FINAL": "Cuartos de final",
    "SEMI_FINAL": "Semifinales",
    "THIRD_PLACE": "Tercer puesto",
    "FINAL": "Final",
}


def _dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stage_label(stage: dict[str, Any]) -> str:
    code = str(stage.get("stage_code") or "").upper()
    if code in STAGE_LABELS:
        return STAGE_LABELS[code]
    name = str(stage.get("stage_name") or "").strip()
    return name or code.replace("_", " ").title()


def _infer_view_type(stage: dict[str, Any]) -> str:
    rules = _as_dict(stage.get("rules"))
    configured = rules.get("view_type")
    if configured:
        return str(configured).upper()

    stage_type = str(stage.get("stage_type") or "").upper()
    code = str(stage.get("stage_code") or "").upper()
    name = str(stage.get("stage_name") or "").upper()
    raw = f"{stage_type} {code} {name}"
    if "GROUP" in raw:
        return "GROUP_TABLES"
    if "LEAGUE" in raw:
        return "LEAGUE_TABLE"
    if any(token in raw for token in ("KNOCKOUT", "ROUND", "FINAL", "SEMI", "QUARTER", "PLAYOFF", "THIRD")):
        return "BRACKET_ROUND"
    return "MATCH_LIST"


def _infer_format_code(season: dict[str, Any], stages: list[dict[str, Any]]) -> str:
    season_metadata = _as_dict(season.get("season_metadata"))
    format_metadata = _as_dict(season_metadata.get("format"))
    if format_metadata.get("type"):
        return str(format_metadata["type"])
    if season.get("format_code"):
        return str(season["format_code"])
    view_types = {_infer_view_type(stage) for stage in stages}
    if "GROUP_TABLES" in view_types and "BRACKET_ROUND" in view_types:
        return "GROUPS_THEN_KNOCKOUT"
    if "LEAGUE_TABLE" in view_types and "BRACKET_ROUND" in view_types:
        return "LEAGUE_PHASE_THEN_KNOCKOUT"
    if "LEAGUE_TABLE" in view_types:
        return "LEAGUE"
    if "BRACKET_ROUND" in view_types:
        return "KNOCKOUT"
    return "CUSTOM"


def _navigation_item(key: str, label: str, enabled: bool, order: int) -> dict[str, Any]:
    return {"key": key, "label": label, "enabled": enabled, "order": order}


def _group_label(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("grupo "):
        return raw
    return raw.replace("_", " ") or "Grupo"


async def _fetch_one(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    result = await conn.execute(text(sql), params)
    row = result.first()
    return _dict(row) if row else None


async def _fetch_all(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = await conn.execute(text(sql), params)
    return [_dict(row) for row in result]


@router.get("/catalog")
async def competition_catalog() -> dict[str, Any]:
    entries = []
    for entry in supported_competitions():
        entries.append(
            {
                "competition_season_slug": entry.slug,
                "competition_slug": entry.competition_slug,
                "name": entry.name,
                "season_label": entry.season_label,
                "competition_type": entry.competition_type,
                "domain_type": entry.competition_metadata.get("domain_type"),
                "format_code": entry.format_code,
                "country_code": entry.country_code,
                "region": entry.region,
                "tier": entry.tier,
                "is_international": entry.is_international,
                "primary_source": entry.source.primary,
                "secondary_sources": entry.source.secondary,
                "stage_count": len(entry.stages),
                "group_count": len(entry.groups),
            }
        )
    return {"ok": True, "data": {"competitions": entries}}


@router.get("/{competition_season_id}/layout", response_model=CompetitionLayoutEnvelope)
async def competition_layout(
    competition_season_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> dict[str, Any]:
    season = await _fetch_one(
        conn,
        """
        select
          cs.competition_season_id::text,
          cs.slug as competition_season_slug,
          cs.season_label,
          cs.starts_at,
          cs.ends_at,
          cs.timezone_name,
          cs.status,
          cs.format_code,
          cs.metadata as season_metadata,
          c.competition_id::text,
          c.slug as competition_slug,
          c.display_name as competition_name,
          c.competition_type,
          c.metadata as competition_metadata
        from competition_seasons cs
        join competitions c on c.competition_id = cs.competition_id
        where cs.slug = :season_ref
           or cs.competition_season_id::text = :season_ref
        limit 1
        """,
        {"season_ref": competition_season_id},
    )
    if not season:
        raise HTTPException(status_code=404, detail="competition season not found")

    params = {"season_id": season["competition_season_id"]}
    stages = await _fetch_all(
        conn,
        """
        select
          st.stage_id::text,
          st.stage_code,
          st.stage_name,
          st.stage_order,
          st.stage_type,
          st.starts_at,
          st.ends_at,
          st.rules,
          count(distinct cg.group_id)::int as group_count,
          count(distinct ts.tournament_slot_id)::int as slot_count,
          count(distinct m.match_id)::int as match_count
        from competition_stages st
        left join competition_groups cg on cg.stage_id = st.stage_id
        left join tournament_slots ts on ts.stage_id = st.stage_id
        left join matches m on m.stage_id = st.stage_id
        where st.competition_season_id = cast(:season_id as uuid)
        group by st.stage_id
        order by st.stage_order, st.stage_code
        """,
        params,
    )
    groups = await _fetch_all(
        conn,
        """
        select
          group_id::text,
          stage_id::text,
          group_code,
          group_name,
          group_order,
          metadata
        from competition_groups
        where competition_season_id = cast(:season_id as uuid)
        order by group_order nulls last, group_code
        """,
        params,
    )
    slots = await _fetch_all(
        conn,
        """
        select
          tournament_slot_id::text,
          stage_id::text,
          slot_code,
          slot_label,
          slot_type,
          source_group_id::text,
          source_match_id::text,
          source_rank,
          resolved_team_id::text,
          resolved_at,
          metadata
        from tournament_slots
        where competition_season_id = cast(:season_id as uuid)
        order by slot_code
        """,
        params,
    )
    groups_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in groups:
        groups_by_stage[group["stage_id"]].append(
            {
                "group_id": group["group_id"],
                "group_code": group["group_code"],
                "group_name": group["group_name"],
                "group_label": _group_label(group["group_name"] or group["group_code"]),
                "group_order": group["group_order"],
                "metadata": _as_dict(group.get("metadata")),
            }
        )
    slots_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for slot in slots:
        if not slot.get("stage_id"):
            continue
        slots_by_stage[slot["stage_id"]].append(
            {
                "tournament_slot_id": slot["tournament_slot_id"],
                "slot_code": slot["slot_code"],
                "slot_label": slot["slot_label"],
                "slot_type": slot["slot_type"],
                "source_group_id": slot["source_group_id"],
                "source_match_id": slot["source_match_id"],
                "source_rank": slot["source_rank"],
                "resolved_team_id": slot["resolved_team_id"],
                "resolved_at": slot["resolved_at"],
                "metadata": _as_dict(slot.get("metadata")),
            }
        )

    standings_count = await _fetch_one(
        conn,
        "select count(*)::int as count from standings where competition_season_id = cast(:season_id as uuid)",
        params,
    )
    team_count = await _fetch_one(
        conn,
        "select count(*)::int as count from competition_team_entries where competition_season_id = cast(:season_id as uuid)",
        params,
    )

    stage_dtos = []
    view_type_counts: dict[str, int] = defaultdict(int)
    for stage in stages:
        rules = _as_dict(stage.get("rules"))
        view_type = _infer_view_type(stage)
        view_type_counts[view_type] += 1
        stage_dtos.append(
            {
                "stage_id": stage["stage_id"],
                "stage_code": stage["stage_code"],
                "stage_label": rules.get("label") or _stage_label(stage),
                "stage_name": stage["stage_name"],
                "stage_type": stage["stage_type"],
                "stage_order": stage["stage_order"],
                "display_order": stage["stage_order"],
                "view_type": view_type,
                "has_groups": bool(stage.get("group_count")),
                "has_slots": bool(stage.get("slot_count")),
                "match_count": stage.get("match_count") or 0,
                "expected_match_count": rules.get("expected_matches"),
                "groups": groups_by_stage.get(stage["stage_id"], []),
                "slots": slots_by_stage.get(stage["stage_id"], []),
                "rules": rules,
            }
        )

    has_groups = any(stage["has_groups"] for stage in stage_dtos)
    has_knockout = view_type_counts.get("BRACKET_ROUND", 0) > 0
    has_league_table = view_type_counts.get("LEAGUE_TABLE", 0) > 0
    has_standings = bool((standings_count or {}).get("count")) or has_groups or has_league_table
    has_teams = bool((team_count or {}).get("count"))

    season_metadata = _as_dict(season.get("season_metadata"))
    competition_metadata = _as_dict(season.get("competition_metadata"))
    ui_metadata = _as_dict(season_metadata.get("ui")) or _as_dict(competition_metadata.get("ui"))
    configured_nav = ui_metadata.get("navigation") if isinstance(ui_metadata.get("navigation"), list) else None

    fallback_nav = [
        _navigation_item("matches", "Partidos", True, 10),
        _navigation_item("standings", "Posiciones", has_standings, 20),
        _navigation_item("teams", "Equipos", has_teams, 30),
        _navigation_item("bracket", "Eliminatorias", has_knockout, 40),
    ]
    fallback_by_key = {item["key"]: item for item in fallback_nav}
    navigation = []
    if configured_nav:
        for index, item in enumerate(configured_nav):
            if isinstance(item, str):
                key = item
                item = {"key": item}
            elif isinstance(item, dict) and item.get("key"):
                key = str(item["key"])
            else:
                continue
            base = fallback_by_key.get(key, _navigation_item(key, key.replace("_", " ").title(), True, index * 10))
            navigation.append(
                {
                    **base,
                    "label": item.get("label") or base["label"],
                    "enabled": bool(item.get("enabled", base["enabled"])),
                    "order": int(item.get("order", base["order"])),
                }
            )
    else:
        navigation = fallback_nav
    navigation = sorted([item for item in navigation if item["enabled"]], key=lambda item: item["order"])

    default_view = str(ui_metadata.get("default_view") or (navigation[0]["key"] if navigation else "matches"))
    if default_view not in {item["key"] for item in navigation} and navigation:
        default_view = navigation[0]["key"]

    layout = {
        "competition": {
            "competition_id": season["competition_id"],
            "slug": season["competition_slug"],
            "display_name": season["competition_name"],
            "competition_type": season["competition_type"],
        },
        "season": {
            "competition_season_id": season["competition_season_id"],
            "slug": season["competition_season_slug"],
            "season_label": season["season_label"],
            "status": season["status"],
            "timezone_name": season["timezone_name"],
            "starts_at": season["starts_at"],
            "ends_at": season["ends_at"],
            "format_code": _infer_format_code(season, stages),
        },
        "competition_season_id": season["competition_season_id"],
        "name": f'{season["competition_name"]} {season["season_label"]}'.strip(),
        "competition_type": season["competition_type"],
        "format_code": _infer_format_code(season, stages),
        "navigation": navigation,
        "capabilities": {
            "has_groups": has_groups,
            "has_league_table": has_league_table,
            "has_knockout": has_knockout,
            "has_standings": has_standings,
            "has_teams": has_teams,
        },
        "ui": {
            "default_view": default_view,
            "navigation": navigation,
        },
        "stages": stage_dtos,
        "metadata": {
            "format": _as_dict(season_metadata.get("format")),
            "ui": ui_metadata,
        },
    }
    return {"ok": True, "data": layout}

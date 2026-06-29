from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
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


def _normalize_rating_type(value: str | None) -> str:
    raw = str(value or "ELO_INTERNATIONAL").strip().upper()
    aliases = {
        "GLOBAL": "ELO_GLOBAL",
        "INTERNATIONAL": "ELO_INTERNATIONAL",
        "DOMESTIC": "ELO_DOMESTIC",
        "HOME": "ELO_HOME",
        "AWAY": "ELO_AWAY",
        "ELO_GLOBAL": "ELO_GLOBAL",
        "ELO_INTERNATIONAL": "ELO_INTERNATIONAL",
        "ELO_DOMESTIC": "ELO_DOMESTIC",
    }
    return aliases.get(raw, raw)


def _elo_public_label(rating_type: str) -> str:
    return {
        "ELO_GLOBAL": "GLOBAL",
        "ELO_INTERNATIONAL": "INTERNATIONAL",
        "ELO_DOMESTIC": "DOMESTIC",
        "ELO_HOME": "HOME",
        "ELO_AWAY": "AWAY",
    }.get(rating_type, rating_type)


async def _fetch_one(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    result = await conn.execute(text(sql), params)
    row = result.first()
    return _dict(row) if row else None


async def _fetch_all(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = await conn.execute(text(sql), params)
    return [_dict(row) for row in result]


async def _resolve_season_ref(conn: AsyncConnection, season_ref: str) -> dict[str, Any]:
    season = await _fetch_one(
        conn,
        """
        select
          cs.competition_season_id::text as competition_season_id,
          cs.slug as competition_season_slug,
          cs.season_label,
          cs.status,
          c.display_name as competition_name
        from competition_seasons cs
        join competitions c on c.competition_id = cs.competition_id
        where cs.slug = :season_ref
           or cs.competition_season_id::text = :season_ref
        limit 1
        """,
        {"season_ref": season_ref},
    )
    if not season:
        raise HTTPException(status_code=404, detail="competition season not found")
    return season


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


@router.get("/{competition_season_id}/standings/global")
async def competition_standings_global(
        competition_season_id: str,
        conn: AsyncConnection = Depends(get_connection),
) -> dict[str, Any]:
        season = await _resolve_season_ref(conn, competition_season_id)

        rows = await _fetch_all(
                conn,
                """
                with season as (
                    select cast(:season_id as uuid) as competition_season_id
                ),
                entries as (
                    select
                        cte.team_id::text as team_id,
                        t.slug as team_slug,
                        t.display_name as team_name,
                        t.country_code,
                        c.flag_emoji,
                        c.fifa_code as country_fifa_code,
                        c.continent,
                        c.region as country_region
                    from competition_team_entries cte
                    join season s on s.competition_season_id = cte.competition_season_id
                    join teams t on t.team_id = cte.team_id
                    left join countries c on c.code_alpha2 = t.country_code
                ),
                latest as (
                    select distinct on (st.team_id)
                        st.team_id::text as team_id,
                        st.position as stage_position,
                        st.played,
                        st.wins,
                        st.draws,
                        st.losses,
                        st.goals_for,
                        st.goals_against,
                        st.goal_difference,
                        st.points,
                        st.qualification_status,
                        cg.group_code,
                        cg.group_name,
                        cs.stage_code,
                        cs.stage_name,
                        st.as_of
                    from standings st
                    join season s on s.competition_season_id = st.competition_season_id
                    left join competition_groups cg on cg.group_id = st.group_id
                    left join competition_stages cs on cs.stage_id = st.stage_id
                    order by st.team_id, st.as_of desc nulls last
                ),
                ranked as (
                    select
                        e.*,
                        l.stage_position,
                        l.played,
                        l.wins,
                        l.draws,
                        l.losses,
                        l.goals_for,
                        l.goals_against,
                        l.goal_difference,
                        l.points,
                        l.qualification_status,
                        l.group_code,
                        l.group_name,
                        l.stage_code,
                        l.stage_name,
                        row_number() over (
                            order by
                                coalesce(l.points, 0) desc,
                                coalesce(l.goal_difference, 0) desc,
                                coalesce(l.goals_for, 0) desc,
                                coalesce(l.played, 9999) asc,
                                lower(e.team_name) asc
                        ) as global_position
                    from entries e
                    left join latest l on l.team_id = e.team_id
                )
                select *
                from ranked
                order by global_position
                """,
                {"season_id": season["competition_season_id"]},
        )

        teams = []
        for row in rows:
                teams.append(
                        {
                                "global_position": row["global_position"],
                                "team_id": row["team_id"],
                                "team_slug": row["team_slug"],
                                "team_name": row["team_name"],
                                "country_code": row["country_code"],
                                "flag_emoji": row["flag_emoji"],
                                "fifa_code": row["country_fifa_code"],
                                "group_code": row["group_code"],
                                "group_name": row["group_name"],
                                "stage_code": row["stage_code"],
                                "stage_name": row["stage_name"],
                                "stage_position": row["stage_position"],
                                "points": row["points"] or 0,
                                "played": row["played"] or 0,
                                "wins": row["wins"] or 0,
                                "draws": row["draws"] or 0,
                                "losses": row["losses"] or 0,
                                "goals_for": row["goals_for"] or 0,
                                "goals_against": row["goals_against"] or 0,
                                "goal_difference": row["goal_difference"] or 0,
                                "status": row["qualification_status"] or "PENDING",
                        }
                )

        return {
                "ok": True,
                "data": {
                        "competition_season_id": season["competition_season_id"],
                        "season_slug": season["competition_season_slug"],
                        "season_label": season.get("season_label"),
                        "competition_name": season.get("competition_name"),
                        "teams": teams,
                },
        }


@router.get("/{competition_season_id}/teams")
async def competition_teams_catalog(
        competition_season_id: str,
        search: str | None = Query(default=None),
        sort: str = Query(default="name"),
        group: str | None = Query(default=None),
        status: str | None = Query(default=None),
        country: str | None = Query(default=None),
        continent: str | None = Query(default=None),
        elo_rating_type: str = Query(default="ELO_GLOBAL"),
        conn: AsyncConnection = Depends(get_connection),
) -> dict[str, Any]:
        season = await _resolve_season_ref(conn, competition_season_id)
        rating_type = _normalize_rating_type(elo_rating_type)

        rows = await _fetch_all(
                conn,
                """
                with season as (
                    select cast(:season_id as uuid) as competition_season_id
                ),
                entries as (
                    select
                        cte.team_id::text as team_id,
                        t.slug as team_slug,
                        t.display_name as team_name,
                        t.country_code,
                        c.flag_emoji,
                        c.fifa_code as country_fifa_code,
                        c.continent,
                        c.region as country_region
                    from competition_team_entries cte
                    join season s on s.competition_season_id = cte.competition_season_id
                    join teams t on t.team_id = cte.team_id
                    left join countries c on c.code_alpha2 = t.country_code
                ),
                latest_standings as (
                    select distinct on (st.team_id)
                        st.team_id::text as team_id,
                        st.position as stage_position,
                        st.played,
                        st.wins,
                        st.draws,
                        st.losses,
                        st.goals_for,
                        st.goals_against,
                        st.goal_difference,
                        st.points,
                        st.qualification_status,
                        cg.group_code,
                        cg.group_name,
                        cs.stage_code,
                        cs.stage_name,
                        st.as_of
                    from standings st
                    join season s on s.competition_season_id = st.competition_season_id
                    left join competition_groups cg on cg.group_id = st.group_id
                    left join competition_stages cs on cs.stage_id = st.stage_id
                    order by st.team_id, st.as_of desc nulls last
                ),
                latest_elo as (
                    select distinct on (rs.team_id)
                        rs.team_id::text as team_id,
                        rs.rating_value as rating_value,
                        rs.as_of
                    from rating_snapshots rs
                    join season s on s.competition_season_id = rs.competition_season_id
                    where rs.rating_type = :rating_type
                    order by rs.team_id, rs.as_of desc
                ),
                roster as (
                    select
                        cr.team_id::text as team_id,
                        count(distinct cr.player_id)::int as roster_count
                    from competition_rosters cr
                    join season s on s.competition_season_id = cr.competition_season_id
                    group by cr.team_id
                )
                select
                    e.*,
                    ls.stage_position,
                    ls.played,
                    ls.wins,
                    ls.draws,
                    ls.losses,
                    ls.goals_for,
                    ls.goals_against,
                    ls.goal_difference,
                    ls.points,
                    ls.qualification_status,
                    ls.group_code,
                    ls.group_name,
                    ls.stage_code,
                    ls.stage_name,
                    le.rating_value as elo_rating,
                    coalesce(r.roster_count, 0) as roster_count,
                    row_number() over (
                        order by
                            coalesce(ls.points, 0) desc,
                            coalesce(ls.goal_difference, 0) desc,
                            coalesce(ls.goals_for, 0) desc,
                            coalesce(ls.played, 9999) asc,
                            lower(e.team_name) asc
                    ) as global_position
                from entries e
                left join latest_standings ls on ls.team_id = e.team_id
                left join latest_elo le on le.team_id = e.team_id
                left join roster r on r.team_id = e.team_id
                """,
                {"season_id": season["competition_season_id"], "rating_type": rating_type},
        )

        normalized_search = str(search or "").strip().lower()
        normalized_group = str(group or "").strip().lower()
        normalized_status = str(status or "").strip().upper()
        normalized_country = str(country or "").strip().upper()
        normalized_continent = str(continent or "").strip().lower()

        filtered = []
        for row in rows:
                if normalized_search and normalized_search not in str(row.get("team_name") or "").lower():
                        continue
                if normalized_group:
                        group_candidate = str(row.get("group_code") or row.get("group_name") or "").lower()
                        if normalized_group not in group_candidate:
                                continue
                if normalized_status and normalized_status != str(row.get("qualification_status") or "PENDING").upper():
                        continue
                if normalized_country and normalized_country != str(row.get("country_code") or "").upper():
                        continue
                if normalized_continent and normalized_continent != str(row.get("continent") or "").lower():
                        continue
                filtered.append(row)

        sort_key = str(sort or "name").strip().lower()
        if sort_key == "position":
                filtered.sort(key=lambda r: (r.get("global_position") or 9999, str(r.get("team_name") or "").lower()))
        elif sort_key == "points":
                filtered.sort(
                        key=lambda r: (
                                -(r.get("points") or 0),
                                -(r.get("goal_difference") or 0),
                                -(r.get("goals_for") or 0),
                                r.get("played") or 9999,
                                str(r.get("team_name") or "").lower(),
                        )
                )
        elif sort_key == "elo":
                filtered.sort(key=lambda r: (-(r.get("elo_rating") or 0), str(r.get("team_name") or "").lower()))
        else:
                filtered.sort(key=lambda r: str(r.get("team_name") or "").lower())

        teams = []
        for row in filtered:
                teams.append(
                        {
                                "team_id": row["team_id"],
                                "team_slug": row["team_slug"],
                                "team_name": row["team_name"],
                                "slug": row["team_slug"],
                                "display_name": row["team_name"],
                                "flag_emoji": row["flag_emoji"],
                                "fifa_code": row.get("country_fifa_code"),
                                "country_code": row.get("country_code"),
                                "continent": row.get("continent"),
                                "region": row.get("country_region"),
                                "group_code": row.get("group_code"),
                                "group_name": row.get("group_name"),
                                "stage_code": row.get("stage_code"),
                                "stage_name": row.get("stage_name"),
                                "global_position": row.get("global_position"),
                                "position": row.get("stage_position"),
                                "points": row.get("points") or 0,
                                "played": row.get("played") or 0,
                                "wins": row.get("wins") or 0,
                                "draws": row.get("draws") or 0,
                                "losses": row.get("losses") or 0,
                                "goals_for": row.get("goals_for") or 0,
                                "goals_against": row.get("goals_against") or 0,
                                "goal_difference": row.get("goal_difference") or 0,
                                "elo_rating": row.get("elo_rating"),
                                "roster_count": row.get("roster_count") or 0,
                                "status": row.get("qualification_status") or "PENDING",
                        }
                )

        available_groups = sorted({str(r.get("group_code") or "").strip() for r in rows if r.get("group_code")})
        available_statuses = sorted({str(r.get("qualification_status") or "PENDING").strip() for r in rows})
        available_countries = sorted({str(r.get("country_code") or "").strip() for r in rows if r.get("country_code")})
        available_continents = sorted({str(r.get("continent") or "").strip() for r in rows if r.get("continent")})

        return {
                "ok": True,
                "data": {
                        "competition_season_id": season["competition_season_id"],
                        "season_slug": season["competition_season_slug"],
                        "rating_type": _elo_public_label(rating_type),
                        "teams": teams,
                        "filters": {
                                "search": search,
                                "sort": sort_key,
                                "group": group,
                                "status": status,
                                "country": country,
                                "continent": continent,
                        },
                        "available_filters": {
                                "groups": available_groups,
                                "statuses": available_statuses,
                                "countries": available_countries,
                                "continents": available_continents,
                        },
                },
        }


@router.get("/{competition_season_id}/elo")
async def competition_elo_ranking(
        competition_season_id: str,
        rating_type: str = Query(default="INTERNATIONAL"),
        conn: AsyncConnection = Depends(get_connection),
) -> dict[str, Any]:
        season = await _resolve_season_ref(conn, competition_season_id)
        normalized_rating_type = _normalize_rating_type(rating_type)

        available_types_rows = await _fetch_all(
                conn,
                """
                select distinct rs.rating_type
                from rating_snapshots rs
                where rs.competition_season_id = cast(:season_id as uuid)
                order by rs.rating_type
                """,
                {"season_id": season["competition_season_id"]},
        )
        available_types = [row["rating_type"] for row in available_types_rows]

        if normalized_rating_type not in available_types and available_types:
                if "ELO_INTERNATIONAL" in available_types:
                        normalized_rating_type = "ELO_INTERNATIONAL"
                elif "ELO_GLOBAL" in available_types:
                        normalized_rating_type = "ELO_GLOBAL"
                else:
                        normalized_rating_type = available_types[0]

        rows = await _fetch_all(
                conn,
                """
                with season as (
                    select cast(:season_id as uuid) as competition_season_id
                ),
                entries as (
                    select
                        cte.team_id::text as team_id,
                        t.slug as team_slug,
                        t.display_name as team_name,
                        t.country_code,
                        c.flag_emoji
                    from competition_team_entries cte
                    join season s on s.competition_season_id = cte.competition_season_id
                    join teams t on t.team_id = cte.team_id
                    left join countries c on c.code_alpha2 = t.country_code
                ),
                ratings as (
                    select
                        rs.team_id::text as team_id,
                        rs.rating_value,
                        rs.as_of,
                        row_number() over (partition by rs.team_id order by rs.as_of desc) as rn,
                        lead(rs.rating_value) over (partition by rs.team_id order by rs.as_of desc) as previous_rating
                    from rating_snapshots rs
                    join season s on s.competition_season_id = rs.competition_season_id
                    where rs.rating_type = :rating_type
                ),
                latest as (
                    select
                        team_id,
                        rating_value,
                        previous_rating,
                        as_of
                    from ratings
                    where rn = 1
                ),
                matches_count as (
                    select
                        mp.team_id::text as team_id,
                        count(distinct mp.match_id)::int as matches
                    from match_participants mp
                    join matches m on m.match_id = mp.match_id
                    join season s on s.competition_season_id = m.competition_season_id
                    where m.status = 'FINISHED'
                    group by mp.team_id
                )
                select
                    e.team_id,
                    e.team_slug,
                    e.team_name,
                    e.country_code,
                    e.flag_emoji,
                    l.rating_value,
                    l.previous_rating,
                    l.as_of,
                    coalesce(mc.matches, 0) as matches,
                    row_number() over (
                        order by coalesce(l.rating_value, -1) desc, lower(e.team_name) asc
                    ) as rank
                from entries e
                left join latest l on l.team_id = e.team_id
                left join matches_count mc on mc.team_id = e.team_id
                order by rank
                """,
                {
                        "season_id": season["competition_season_id"],
                        "rating_type": normalized_rating_type,
                },
        )

        updated_at = None
        for row in rows:
                as_of = row.get("as_of")
                if as_of and (updated_at is None or as_of > updated_at):
                        updated_at = as_of

        teams = []
        for row in rows:
                rating = row.get("rating_value")
                previous = row.get("previous_rating")
                teams.append(
                        {
                                "rank": row.get("rank"),
                                "team_id": row.get("team_id"),
                                "team_slug": row.get("team_slug"),
                                "team_name": row.get("team_name"),
                                "country_code": row.get("country_code"),
                                "flag": row.get("flag_emoji"),
                                "rating": float(rating) if rating is not None else None,
                                "previous_rating": float(previous) if previous is not None else None,
                                "delta": (float(rating) - float(previous)) if rating is not None and previous is not None else None,
                                "matches": row.get("matches") or 0,
                        }
                )

        return {
                "ok": True,
                "data": {
                        "competition_season_id": season["competition_season_id"],
                        "season_slug": season["competition_season_slug"],
                        "rating_type": _elo_public_label(normalized_rating_type),
                        "rating_types": [_elo_public_label(rt) for rt in available_types],
                        "updated_at": updated_at.isoformat() if updated_at else None,
                        "teams": teams,
                },
        }


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
    has_tournament = has_groups or has_knockout or has_league_table

    rating_count = await _fetch_one(
        conn,
        "select count(*)::int as count from rating_snapshots where competition_season_id = cast(:season_id as uuid)",
        params,
    )
    has_elo = bool((rating_count or {}).get("count"))

    season_metadata = _as_dict(season.get("season_metadata"))
    competition_metadata = _as_dict(season.get("competition_metadata"))
    ui_metadata = _as_dict(season_metadata.get("ui")) or _as_dict(competition_metadata.get("ui"))
    configured_nav = ui_metadata.get("navigation") if isinstance(ui_metadata.get("navigation"), list) else None

    fallback_nav = [
        _navigation_item("matches", "Partidos", True, 10),
        _navigation_item("standings", "Posiciones", has_standings, 20),
        _navigation_item("teams", "Equipos", has_teams, 30),
        _navigation_item("tournament", "Torneo", has_tournament, 40),
        _navigation_item("news", "Noticias", True, 50),
        _navigation_item("elo", "ELO", has_elo, 60),
        _navigation_item("ev", "EV+", True, 70),
        _navigation_item("model", "Modelo", True, 80),
        _navigation_item("stats", "Stats", True, 90),
        _navigation_item("bracket", "Eliminatorias", has_knockout, 400),
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

    tournament_views: list[dict[str, Any]] = []

    has_group_tables = any(str(stage["view_type"]).upper() == "GROUP_TABLES" for stage in stage_dtos)
    has_league_table_view = any(str(stage["view_type"]).upper() == "LEAGUE_TABLE" for stage in stage_dtos)
    has_match_list = any(str(stage["view_type"]).upper() == "MATCH_LIST" for stage in stage_dtos)
    has_bracket = any(str(stage["view_type"]).upper() == "BRACKET_ROUND" for stage in stage_dtos)

    order = 10
    if has_group_tables:
        tournament_views.append({"key": "groups", "label": "Grupos", "render_mode": "GROUP_TABLES", "enabled": True, "order": order})
        order += 10
    if has_league_table_view:
        tournament_views.append({"key": "table", "label": "Tabla", "render_mode": "LEAGUE_TABLE", "enabled": True, "order": order})
        order += 10
    if has_match_list:
        tournament_views.append({"key": "fixtures", "label": "Fechas", "render_mode": "MATCH_LIST", "enabled": True, "order": order})
        order += 10
    if has_bracket:
        tournament_views.append({"key": "knockout", "label": "Eliminatoria", "render_mode": "BRACKET", "enabled": True, "order": order})
        order += 10
    if has_groups:
        tournament_views.append({"key": "qualified", "label": "Clasificados", "render_mode": "QUALIFICATION_SUMMARY", "enabled": True, "order": order})

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
            "has_tournament": has_tournament,
            "has_elo": has_elo,
        },
        "ui": {
            "default_view": default_view,
            "navigation": navigation,
        },
        "stages": stage_dtos,
        "tournament_views": sorted(tournament_views, key=lambda item: item["order"]),
        "metadata": {
            "format": _as_dict(season_metadata.get("format")),
            "ui": ui_metadata,
        },
    }
    return {"ok": True, "data": layout}


@router.get("/{slug}/qualification-picture")
async def qualification_picture(
    slug: str,
    conn: AsyncConnection = Depends(get_connection),
) -> dict[str, Any]:
    """
    Returns the current qualification picture for a competition season:
    - Group standings with qualification_status per team
    - Best-third ranking (QUALIFIED_BEST_THIRD / THIRD_PLACE_CANDIDATE)
    - Resolved tournament slots (group winners, runners-up, thirds, knockout progression)
    """
    # Resolve season
    season = await _fetch_one(
        conn,
        """
        SELECT competition_season_id::text, slug, status
        FROM competition_seasons
        WHERE slug = :slug OR competition_season_id::text = :slug
        LIMIT 1
        """,
        {"slug": slug},
    )
    if not season:
        raise HTTPException(status_code=404, detail="competition season not found")

    season_id = season["competition_season_id"]

    # Group standings with qualification_status
    group_standings_rows = await _fetch_all(
        conn,
        """
        SELECT DISTINCT ON (s.group_id, s.team_id)
            cg.group_code,
            cg.group_name,
            t.team_id::text,
            t.display_name AS team_name,
            t.country_code,
            s.position,
            s.points,
            s.played,
            s.wins,
            s.draws,
            s.losses,
            s.goals_for,
            s.goals_against,
            s.goal_difference,
            s.qualification_status
        FROM standings s
        JOIN competition_groups cg ON cg.group_id = s.group_id
        JOIN teams t ON t.team_id = s.team_id
        WHERE s.competition_season_id = cast(:sid as uuid)
          AND s.group_id IS NOT NULL
        ORDER BY s.group_id, s.team_id, s.as_of DESC NULLS LAST
        """,
        {"sid": season_id},
    )

    # Organize by group
    groups: dict[str, dict] = {}
    for row in group_standings_rows:
        gc = row["group_code"]
        if gc not in groups:
            groups[gc] = {"group_code": gc, "group_name": row["group_name"], "teams": []}
        groups[gc]["teams"].append({
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "country_code": row["country_code"],
            "position": row["position"],
            "points": row["points"],
            "played": row["played"],
            "wins": row["wins"],
            "draws": row["draws"],
            "losses": row["losses"],
            "goals_for": row["goals_for"],
            "goals_against": row["goals_against"],
            "goal_difference": row["goal_difference"],
            "qualification_status": row["qualification_status"] or "PENDING",
        })
    for g in groups.values():
        g["teams"].sort(key=lambda t: (t["position"] or 99))

    # Best-third ranking
    third_rows = await _fetch_all(
        conn,
        """
        SELECT DISTINCT ON (s.group_id, s.team_id)
            t.team_id::text,
            t.display_name AS team_name,
            t.country_code,
            cg.group_code,
            s.points,
            s.goal_difference,
            s.goals_for,
            s.qualification_status
        FROM standings s
        JOIN teams t ON t.team_id = s.team_id
        JOIN competition_groups cg ON cg.group_id = s.group_id
        WHERE s.competition_season_id = cast(:sid as uuid)
          AND s.qualification_status IN ('THIRD_PLACE_CANDIDATE', 'QUALIFIED_BEST_THIRD', 'PENDING_TIEBREAKER')
          AND s.group_id IS NOT NULL
        ORDER BY s.group_id, s.team_id, s.as_of DESC NULLS LAST
        """,
        {"sid": season_id},
    )
    thirds = sorted(
        [dict(r) for r in third_rows],
        key=lambda r: (
            0 if r["qualification_status"] == "QUALIFIED_BEST_THIRD" else 1,
            -(r["points"] or 0),
            -(r["goal_difference"] or 0),
            -(r["goals_for"] or 0),
        ),
    )

    # Tournament slots
    slot_rows = await _fetch_all(
        conn,
        """
        SELECT
            ts.slot_code,
            ts.slot_label,
            ts.slot_type,
            ts.source_rank,
            ts.resolved_team_id::text,
            ts.resolved_at,
            ts.metadata,
            t.display_name AS team_name,
            t.country_code
        FROM tournament_slots ts
        LEFT JOIN teams t ON t.team_id = ts.resolved_team_id
        WHERE ts.competition_season_id = cast(:sid as uuid)
        ORDER BY ts.slot_code
        """,
        {"sid": season_id},
    )

    slots = [
        {
            "slot_code": r["slot_code"],
            "slot_label": r["slot_label"],
            "slot_type": r["slot_type"],
            "resolved": r["resolved_team_id"] is not None,
            "resolved_team_id": r["resolved_team_id"],
            "team_name": r["team_name"],
            "country_code": r["country_code"],
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        }
        for r in slot_rows
    ]

    return {
        "ok": True,
        "data": {
            "competition_season_id": season_id,
            "slug": slug,
            "groups": list(groups.values()),
            "best_thirds": thirds,
            "tournament_slots": slots,
            "summary": {
                "groups_total": len(groups),
                "slots_total": len(slots),
                "slots_resolved": sum(1 for s in slots if s["resolved"]),
                "thirds_qualified": sum(1 for t in thirds if t["qualification_status"] == "QUALIFIED_BEST_THIRD"),
            },
        },
    }

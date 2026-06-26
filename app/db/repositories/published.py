from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.types import DateTime

from app.db.repositories.base import Repository


class PublishedRepository(Repository):
    def _coerce_datetime(self, value: datetime | str | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    async def match_schedule(self, season: str, kickoff_from: datetime | str | None = None, kickoff_to: datetime | str | None = None) -> list[dict[str, Any]]:
        kickoff_from = self._coerce_datetime(kickoff_from)
        kickoff_to = self._coerce_datetime(kickoff_to)
        filters = ["cs.slug = :season"]
        params = {"season": season}
        if kickoff_from:
            filters.append("m.kickoff_at >= cast(:kickoff_from as timestamptz)")
            params["kickoff_from"] = kickoff_from
        if kickoff_to:
            filters.append("m.kickoff_at < cast(:kickoff_to as timestamptz)")
            params["kickoff_to"] = kickoff_to
        where_clause = "\n            and ".join(filters)
        sql = f"""
          select
            m.match_id::text,
            cs.slug as competition_season_slug,
            c.display_name as competition_name,
            m.slug,
            m.match_number,
            m.kickoff_at,
            m.status,
            m.is_neutral,
            m.home_score,
            m.away_score,
            m.stage_id::text as match_stage_id,
            m.group_id::text as match_group_id,
            st.stage_id::text as stage_id,
            st.stage_code,
            st.stage_name,
            st.stage_type,
            st.rules as stage_rules,
            coalesce(
              st.rules->>'view_type',
              case
                when st.stage_type in ('GROUP_STAGE') then 'GROUP_TABLES'
                when st.stage_type in ('LEAGUE_PHASE') then 'LEAGUE_TABLE'
                when st.stage_type in ('KNOCKOUT', 'THIRD_PLACE', 'FINAL') then 'BRACKET_ROUND'
                else 'MATCH_LIST'
              end
            ) as stage_view_type,
            cg.group_id::text,
            cg.group_code,
            cg.group_name,
            cg.group_order,
            home.team_id::text as home_team_id,
            home.participant_role as home_participant_role,
            home_team.slug as home_team_slug,
            home_team.display_name as home_team_name,
            home_team.country_code as home_country_code,
            home_team.metadata as home_team_metadata,
            home_country.flag_emoji as home_flag_emoji,
            home_country.fifa_code as home_country_fifa_code,
            home_slot.slot_code as home_slot_code,
            home_slot.slot_label as home_slot_label,
            away.team_id::text as away_team_id,
            away.participant_role as away_participant_role,
            away_team.slug as away_team_slug,
            away_team.display_name as away_team_name,
            away_team.country_code as away_country_code,
            away_team.metadata as away_team_metadata,
            away_country.flag_emoji as away_flag_emoji,
            away_country.fifa_code as away_country_fifa_code,
            away_slot.slot_code as away_slot_code,
            away_slot.slot_label as away_slot_label,
            v.venue_id::text,
            v.slug as venue_slug,
            v.display_name as venue_name,
            v.city as venue_city,
            v.country_code as venue_country_code,
            venue_country.flag_emoji as venue_flag_emoji,
            v.timezone_name as venue_timezone,
            v.latitude as venue_latitude,
            v.longitude as venue_longitude,
            m.winner_team_id::text,
            m.metadata
          from matches m
          join competition_seasons cs on cs.competition_season_id = m.competition_season_id
          join competitions c on c.competition_id = cs.competition_id
          left join competition_stages st on st.stage_id = m.stage_id
          left join competition_groups cg on cg.group_id = m.group_id
          left join venues v on v.venue_id = m.venue_id
          left join countries venue_country on venue_country.code_alpha2 = v.country_code
          left join match_participants home on home.match_id = m.match_id and home.side = 'HOME'
          left join teams home_team on home_team.team_id = home.team_id
          left join countries home_country on home_country.code_alpha2 = home_team.country_code
          left join tournament_slots home_slot on home_slot.tournament_slot_id = home.tournament_slot_id
          left join match_participants away on away.match_id = m.match_id and away.side = 'AWAY'
          left join teams away_team on away_team.team_id = away.team_id
          left join countries away_country on away_country.code_alpha2 = away_team.country_code
          left join tournament_slots away_slot on away_slot.tournament_slot_id = away.tournament_slot_id
          where {where_clause}
          order by m.kickoff_at asc, m.match_number nulls last
        """
        bind_params = [bindparam("season")]
        if "kickoff_from" in params:
            bind_params.append(bindparam("kickoff_from", type_=DateTime(timezone=True)))
        if "kickoff_to" in params:
            bind_params.append(bindparam("kickoff_to", type_=DateTime(timezone=True)))
        statement = text(sql).bindparams(*bind_params)
        result = await self.conn.execute(statement, params)
        return [dict(row._mapping) for row in result]

    async def standings_groups(self, season: str) -> list[dict[str, Any]]:
        return await self.fetch_all(
            """
            select
              cg.group_id::text,
              cg.group_code,
              cg.group_name,
              cg.group_order,
              s.position,
              s.team_id::text,
              t.slug as team_slug,
              t.display_name as team_name,
              t.country_code as team_country_code,
              t.metadata as team_metadata,
              c.flag_emoji,
              c.fifa_code as country_fifa_code,
              s.played,
              s.wins,
              s.draws,
              s.losses,
              s.goals_for,
              s.goals_against,
              s.goal_difference,
              s.points,
              s.source
            from competition_seasons cs
            join competition_groups cg on cg.competition_season_id = cs.competition_season_id
            left join standings s on s.group_id = cg.group_id
            left join teams t on t.team_id = s.team_id
            left join countries c on c.code_alpha2 = t.country_code
            where cs.slug = :season
            order by cg.group_order nulls last, s.position nulls last, t.display_name
            """,
            {"season": season},
        )

    async def ev_opportunities(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.fetch_all("select * from published_ev_opportunities order by decided_at desc limit :limit", {"limit": limit})

    async def blocked_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.fetch_all("select * from published_blocked_decisions order by decided_at desc limit :limit", {"limit": limit})

    async def calibration_summary(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.fetch_all("select * from published_model_calibration order by created_at desc limit :limit", {"limit": limit})

    async def model_diagnostics(self) -> list[dict[str, Any]]:
        return await self.fetch_all("select * from published_model_diagnostics order by model_name, model_version")

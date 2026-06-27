"""
StandingsResolver: recomputes group standings from FINISHED match results.

Ranking rules (read from competition_stages.rules.qualification.group_ranking_rules):
  1. points
  2. goal_difference
  3. goals_for
  4. head_to_head_points       (subset of tied teams only)
  5. head_to_head_goal_difference
  6. head_to_head_goals_for
  7. fair_play_points           (not yet available → PENDING_TIEBREAKER)
  8. drawing_of_lots            (not resolvable automatically → PENDING_TIEBREAKER)

Results are upserted into the standings table with qualification_status.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.qualification.models import (
    ELIMINATED,
    PENDING,
    PENDING_TIEBREAKER,
    QUALIFIED_GROUP_RUNNER_UP,
    QUALIFIED_GROUP_WINNER,
    THIRD_PLACE_CANDIDATE,
    TeamStats,
)

log = logging.getLogger(__name__)

DEFAULT_RANKING_RULES = [
    "points",
    "goal_difference",
    "goals_for",
    "head_to_head_points",
    "head_to_head_goal_difference",
    "head_to_head_goals_for",
]


class StandingsResolver:
    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    async def recompute(self, competition_season_id: str) -> dict[str, Any]:
        """
        Recompute group standings from FINISHED matches and upsert into standings table.
        Returns a summary with group stats and all third-place candidates.
        """
        stage_rules = await self._get_group_stage_rules(competition_season_id)
        direct_qualify = stage_rules.get("qualifies", {}).get("top_n_per_group", 2)
        ranking_rules = stage_rules.get("qualification", {}).get(
            "group_ranking_rules", DEFAULT_RANKING_RULES
        )

        groups = await self._get_groups(competition_season_id)
        if not groups:
            return {"groups_processed": 0, "all_thirds": [], "warning": "no groups found"}

        matches = await self._get_finished_group_matches(competition_season_id)

        # Build match index for H2H lookups: {(team_a, team_b): match}
        match_index: dict[tuple[str, str], dict] = {}
        for m in matches:
            match_index[(m["home_team_id"], m["away_team_id"])] = m

        # Accumulate stats per group
        group_stats: dict[str, dict[str, TeamStats]] = {g["group_id"]: {} for g in groups}
        group_meta: dict[str, dict] = {g["group_id"]: g for g in groups}

        for m in matches:
            gid = m.get("group_id")
            if not gid or gid not in group_stats:
                continue
            home_id = m["home_team_id"]
            away_id = m["away_team_id"]
            home_score = m["home_score"] or 0
            away_score = m["away_score"] or 0
            g = group_meta[gid]

            for tid, tname, gf, ga in [
                (home_id, m["home_name"], home_score, away_score),
                (away_id, m["away_name"], away_score, home_score),
            ]:
                if tid not in group_stats[gid]:
                    group_stats[gid][tid] = TeamStats(
                        team_id=tid, team_name=tname,
                        group_id=gid, group_code=g["group_code"],
                    )
                t = group_stats[gid][tid]
                t.played += 1
                t.goals_for += gf
                t.goals_against += ga
                if gf > ga:
                    t.wins += 1; t.points += 3
                elif gf == ga:
                    t.draws += 1; t.points += 1
                else:
                    t.losses += 1

        all_thirds: list[TeamStats] = []
        groups_processed = 0

        for group_id, team_map in group_stats.items():
            if not team_map:
                continue
            teams = list(team_map.values())
            sorted_teams = self._rank_teams(teams, match_index, ranking_rules)

            for pos, team in enumerate(sorted_teams, 1):
                team.position = pos
                if pos == 1:
                    team.qualification_status = QUALIFIED_GROUP_WINNER
                elif pos == 2:
                    team.qualification_status = QUALIFIED_GROUP_RUNNER_UP
                elif pos == direct_qualify + 1:
                    team.qualification_status = THIRD_PLACE_CANDIDATE
                    all_thirds.append(team)
                else:
                    team.qualification_status = ELIMINATED

            await self._upsert_standings(competition_season_id, sorted_teams)
            groups_processed += 1
            log.info(
                "standings_resolver: group=%s teams=%d",
                group_meta[group_id]["group_code"], len(sorted_teams),
            )

        return {
            "groups_processed": groups_processed,
            "all_thirds": [{"team_id": t.team_id, "group_code": t.group_code,
                             "points": t.points, "goal_difference": t.goal_difference,
                             "goals_for": t.goals_for, "tiebreaker_notes": t.tiebreaker_notes}
                           for t in all_thirds],
        }

    # ── ranking ───────────────────────────────────────────────────────────────

    def _rank_teams(
        self,
        teams: list[TeamStats],
        match_index: dict[tuple[str, str], dict],
        rules: list[str],
    ) -> list[TeamStats]:
        """Sort teams applying ranking rules. Groups with ties that can't be
        resolved by available data are flagged with PENDING_TIEBREAKER."""
        # Initial sort by overall metrics
        teams.sort(key=lambda t: (-t.points, -t.goal_difference, -t.goals_for, t.team_id))

        # Detect and break ties for adjacent teams with same points/GD/GF
        result: list[TeamStats] = []
        i = 0
        while i < len(teams):
            j = i + 1
            # Find run of tied teams
            while j < len(teams) and self._overall_tied(teams[i], teams[j]):
                j += 1
            tied_group = teams[i:j]

            if len(tied_group) > 1:
                tied_group = self._break_tie(tied_group, match_index, rules)

            result.extend(tied_group)
            i = j

        return result

    def _overall_tied(self, a: TeamStats, b: TeamStats) -> bool:
        return a.points == b.points and a.goal_difference == b.goal_difference and a.goals_for == b.goals_for

    def _break_tie(
        self,
        tied: list[TeamStats],
        match_index: dict[tuple[str, str], dict],
        rules: list[str],
    ) -> list[TeamStats]:
        """Apply H2H rules to a tied subset. Falls back to PENDING_TIEBREAKER."""
        if "head_to_head_points" not in rules:
            for t in tied:
                t.tiebreaker_notes.append("PENDING_TIEBREAKER:no_h2h_rule")
                t.qualification_status = PENDING_TIEBREAKER
            return tied

        # Compute H2H stats within the tied subset
        h2h: dict[str, tuple[int, int, int]] = {}  # team_id → (pts, gd, gf)
        for t in tied:
            pts = gd = gf = 0
            for other in tied:
                if other.team_id == t.team_id:
                    continue
                m = match_index.get((t.team_id, other.team_id))
                if m:
                    gf += m["home_score"] or 0
                    gd += (m["home_score"] or 0) - (m["away_score"] or 0)
                    if (m["home_score"] or 0) > (m["away_score"] or 0):
                        pts += 3
                    elif (m["home_score"] or 0) == (m["away_score"] or 0):
                        pts += 1
                m2 = match_index.get((other.team_id, t.team_id))
                if m2:
                    gf += m2["away_score"] or 0
                    gd += (m2["away_score"] or 0) - (m2["home_score"] or 0)
                    if (m2["away_score"] or 0) > (m2["home_score"] or 0):
                        pts += 3
                    elif (m2["home_score"] or 0) == (m2["away_score"] or 0):
                        pts += 1
            h2h[t.team_id] = (pts, gd, gf)

        try:
            tied.sort(key=lambda t: (-h2h[t.team_id][0], -h2h[t.team_id][1], -h2h[t.team_id][2], t.team_id))
        except KeyError:
            pass

        # Check if still tied after H2H — mark as PENDING_TIEBREAKER
        for i in range(len(tied) - 1):
            a, b = tied[i], tied[i + 1]
            if h2h.get(a.team_id) == h2h.get(b.team_id):
                a.tiebreaker_notes.append("PENDING_TIEBREAKER:h2h_equal")
                b.tiebreaker_notes.append("PENDING_TIEBREAKER:h2h_equal")

        return tied

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _get_group_stage_rules(self, competition_season_id: str) -> dict:
        result = await self.conn.execute(
            text("""
                SELECT st.rules
                FROM competition_stages st
                WHERE st.competition_season_id = cast(:sid as uuid)
                  AND st.stage_type = 'GROUP_STAGE'
                LIMIT 1
            """),
            {"sid": competition_season_id},
        )
        row = result.fetchone()
        return row[0] if row and row[0] else {}

    async def _get_groups(self, competition_season_id: str) -> list[dict]:
        result = await self.conn.execute(
            text("""
                SELECT cg.group_id::text, cg.group_code, cg.group_name, cg.group_order
                FROM competition_groups cg
                JOIN competition_seasons cs ON cs.competition_season_id = cg.competition_season_id
                WHERE cg.competition_season_id = cast(:sid as uuid)
                ORDER BY cg.group_order
            """),
            {"sid": competition_season_id},
        )
        return [dict(r._mapping) for r in result]

    async def _get_finished_group_matches(self, competition_season_id: str) -> list[dict]:
        """Get FINISHED matches in GROUP_STAGE, joined to their group via standings."""
        result = await self.conn.execute(
            text("""
                SELECT DISTINCT ON (m.match_id)
                    m.match_id::text,
                    m.home_score,
                    m.away_score,
                    sh.group_id::text,
                    cg.group_code,
                    home_p.team_id::text  AS home_team_id,
                    ht.display_name       AS home_name,
                    away_p.team_id::text  AS away_team_id,
                    at_.display_name      AS away_name
                FROM matches m
                JOIN competition_stages cs
                  ON cs.stage_id = m.stage_id AND cs.stage_type = 'GROUP_STAGE'
                JOIN match_participants home_p
                  ON home_p.match_id = m.match_id
                 AND home_p.side = 'HOME'
                 AND home_p.participant_role = 'TEAM'
                JOIN match_participants away_p
                  ON away_p.match_id = m.match_id
                 AND away_p.side = 'AWAY'
                 AND away_p.participant_role = 'TEAM'
                JOIN teams ht  ON ht.team_id  = home_p.team_id
                JOIN teams at_ ON at_.team_id = away_p.team_id
                -- Link match to group via the home team's standing
                JOIN standings sh
                  ON sh.team_id = home_p.team_id
                 AND sh.competition_season_id = m.competition_season_id
                 AND sh.group_id IS NOT NULL
                JOIN competition_groups cg ON cg.group_id = sh.group_id
                -- Verify away team is in the same group
                JOIN standings sa
                  ON sa.team_id = away_p.team_id
                 AND sa.group_id = sh.group_id
                WHERE m.competition_season_id = cast(:sid as uuid)
                  AND m.status = 'FINISHED'
                ORDER BY m.match_id, sh.as_of DESC
            """),
            {"sid": competition_season_id},
        )
        return [dict(r._mapping) for r in result]

    async def _upsert_standings(
        self,
        competition_season_id: str,
        teams: list[TeamStats],
    ) -> None:
        """Update standings rows with computed stats and qualification_status.
        Only touches rows that have a matching (competition_season_id, group_id, team_id).
        Does not insert new rows — standings seeding is handled by standings_refresh_job."""
        from app.core.time import utc_now
        as_of = utc_now()

        for team in teams:
            await self.conn.execute(
                text("""
                    UPDATE standings SET
                        position            = :position,
                        played              = :played,
                        wins                = :wins,
                        draws               = :draws,
                        losses              = :losses,
                        goals_for           = :goals_for,
                        goals_against       = :goals_against,
                        goal_difference     = :goal_difference,
                        points              = :points,
                        qualification_status = :qs,
                        as_of               = :as_of
                    WHERE competition_season_id = cast(:sid as uuid)
                      AND group_id              = cast(:gid as uuid)
                      AND team_id               = cast(:tid as uuid)
                """),
                {
                    "sid": competition_season_id,
                    "gid": team.group_id,
                    "tid": team.team_id,
                    "position": team.position,
                    "played": team.played,
                    "wins": team.wins,
                    "draws": team.draws,
                    "losses": team.losses,
                    "goals_for": team.goals_for,
                    "goals_against": team.goals_against,
                    "goal_difference": team.goal_difference,
                    "points": team.points,
                    "qs": team.qualification_status,
                    "as_of": as_of,
                },
            )

"""
StandingsResolver: recomputes group standings from FINISHED match results.

FIFA 2026 ranking rules (applied in order):
  1.  points
  2.  H2H points (among tied subset ONLY)
  3.  H2H goal difference (among tied subset ONLY)
  4.  H2H goals scored (among tied subset ONLY)
  5.  If 3+ still tied after H2H: re-apply H2H recursively for still-tied sub-subset
  6.  Overall goal difference
  7.  Overall goals scored
  8.  fair_play_score (less negative = better; 0 if unknown)
  9.  fifa_ranking (lower = better; 999 if unknown)
  10. PENDING_TIEBREAKER (drawing of lots — flag all teams)

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
    "head_to_head_points",
    "head_to_head_goal_difference",
    "head_to_head_goals_for",
    "goal_difference",
    "goals_for",
    "fair_play_score",
    "fifa_ranking",
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

        fair_play_scores = await self._get_fair_play_scores(competition_season_id)
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

            home_rank = m.get("home_fifa_ranking", 999) or 999
            away_rank = m.get("away_fifa_ranking", 999) or 999

            for tid, tname, gf, ga, rank in [
                (home_id, m["home_name"], home_score, away_score, home_rank),
                (away_id, m["away_name"], away_score, home_score, away_rank),
            ]:
                if tid not in group_stats[gid]:
                    group_stats[gid][tid] = TeamStats(
                        team_id=tid, team_name=tname,
                        group_id=gid, group_code=g["group_code"],
                        fair_play_score=fair_play_scores.get(tid, 0),
                        fifa_ranking=rank,
                    )
                t = group_stats[gid][tid]
                t.played += 1
                t.goals_for += gf
                t.goals_against += ga
                t.fifa_ranking = rank
                if gf > ga:
                    t.wins += 1; t.points += 3
                elif gf == ga:
                    t.draws += 1; t.points += 1
                else:
                    t.losses += 1

        all_thirds: list[TeamStats] = []
        decided_groups: list[dict] = []
        groups_processed = 0

        PENDING_STATUSES = {PENDING, PENDING_TIEBREAKER}

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

            # Track fully-decided groups (all teams have non-pending status)
            if (len(sorted_teams) >= 4 and
                    all(t.qualification_status not in PENDING_STATUSES for t in sorted_teams)):
                decided_groups.append({
                    "group_code": sorted_teams[0].group_code,
                    "winner": sorted_teams[0].team_name,
                    "runner_up": sorted_teams[1].team_name,
                    "third": sorted_teams[2].team_name,
                    "eliminated": sorted_teams[3].team_name,
                })

        return {
            "groups_processed": groups_processed,
            "all_thirds": [{"team_id": t.team_id, "group_code": t.group_code,
                             "points": t.points, "goal_difference": t.goal_difference,
                             "goals_for": t.goals_for, "tiebreaker_notes": t.tiebreaker_notes}
                           for t in all_thirds],
            "decided_groups": decided_groups,
        }

    # ── ranking ───────────────────────────────────────────────────────────────

    def _rank_teams(
        self,
        teams: list[TeamStats],
        match_index: dict[tuple[str, str], dict],
        rules: list[str],
    ) -> list[TeamStats]:
        """Sort teams applying full FIFA ranking rules."""
        # Initial sort by points (descending), then stable sort key
        teams.sort(key=lambda t: (-t.points, t.team_id))

        # Find groups of teams tied on points and resolve them
        result: list[TeamStats] = []
        i = 0
        while i < len(teams):
            j = i + 1
            while j < len(teams) and teams[i].points == teams[j].points:
                j += 1
            tied_group = teams[i:j]

            if len(tied_group) > 1:
                tied_group = self._resolve_tied_group(tied_group, match_index)
            else:
                # Single team — still need to set overall GD/GF for consistent ordering
                pass

            result.extend(tied_group)
            i = j

        return result

    def _resolve_tied_group(
        self,
        tied: list[TeamStats],
        match_index: dict[tuple[str, str], dict],
    ) -> list[TeamStats]:
        """
        Apply full FIFA tiebreaker to a group tied on points.
        Steps 2-5: H2H (recursive for sub-ties)
        Steps 6-7: overall GD, overall GF
        Steps 8-9: fair play, FIFA ranking
        Step 10:   PENDING_TIEBREAKER
        """
        # Step 1: Apply H2H to the entire tied group
        after_h2h = self._apply_h2h_recursive(tied, match_index)

        # Flatten and apply overall criteria to remaining tied sub-groups
        result: list[TeamStats] = []
        for sub_group in after_h2h:
            if len(sub_group) == 1:
                result.extend(sub_group)
            else:
                result.extend(self._apply_overall_criteria(sub_group))

        return result

    def _apply_h2h_recursive(
        self,
        tied: list[TeamStats],
        match_index: dict[tuple[str, str], dict],
    ) -> list[list[TeamStats]]:
        """
        Apply H2H among the tied subset. Returns list of sub-groups (each sub-group
        is still internally tied after H2H). Uses recursion for 3+ way ties.
        """
        h2h = self._compute_h2h(tied, match_index)

        # Sort by H2H criteria
        tied.sort(key=lambda t: (
            -h2h[t.team_id][0],  # H2H points
            -h2h[t.team_id][1],  # H2H GD
            -h2h[t.team_id][2],  # H2H GF
            t.team_id,
        ))

        # Group into sub-groups that are still tied on H2H
        sub_groups: list[list[TeamStats]] = []
        i = 0
        while i < len(tied):
            j = i + 1
            while j < len(tied) and h2h[tied[i].team_id] == h2h[tied[j].team_id]:
                j += 1
            sub_group = tied[i:j]
            sub_groups.append(sub_group)
            i = j

        # For sub-groups of 3+ that are still tied, recurse ONLY if the
        # H2H step actually split the group (i.e., the sub-group is smaller
        # than the original tied group). If the sub-group is the same size,
        # we've reached a fixed point — stop recursing.
        resolved_sub_groups: list[list[TeamStats]] = []
        for sub in sub_groups:
            if len(sub) >= 3 and len(sub) < len(tied):
                # Re-apply H2H only within this subset (FIFA rule 5)
                inner = self._apply_h2h_recursive(sub, match_index)
                resolved_sub_groups.extend(inner)
            else:
                resolved_sub_groups.append(sub)

        return resolved_sub_groups

    def _compute_h2h(
        self,
        subset: list[TeamStats],
        match_index: dict[tuple[str, str], dict],
    ) -> dict[str, tuple[int, int, int]]:
        """Compute H2H stats (pts, gd, gf) for each team in the subset."""
        h2h: dict[str, tuple[int, int, int]] = {}
        for t in subset:
            pts = gd = gf = 0
            for other in subset:
                if other.team_id == t.team_id:
                    continue
                m = match_index.get((t.team_id, other.team_id))
                if m:
                    home_score = m["home_score"] or 0
                    away_score = m["away_score"] or 0
                    gf += home_score
                    gd += home_score - away_score
                    if home_score > away_score:
                        pts += 3
                    elif home_score == away_score:
                        pts += 1
                m2 = match_index.get((other.team_id, t.team_id))
                if m2:
                    home_score2 = m2["home_score"] or 0
                    away_score2 = m2["away_score"] or 0
                    gf += away_score2
                    gd += away_score2 - home_score2
                    if away_score2 > home_score2:
                        pts += 3
                    elif away_score2 == home_score2:
                        pts += 1
            h2h[t.team_id] = (pts, gd, gf)
        return h2h

    def _apply_overall_criteria(self, tied: list[TeamStats]) -> list[TeamStats]:
        """
        Apply steps 6-10: overall GD, overall GF, fair_play, fifa_ranking,
        then PENDING_TIEBREAKER.
        """
        # Sort by overall GD, then GF, then fair_play (higher = better = less negative),
        # then FIFA ranking (lower = better)
        tied.sort(key=lambda t: (
            -t.goal_difference,
            -t.goals_for,
            -t.fair_play_score,   # less negative = higher = better
            t.fifa_ranking,       # lower = better
            t.team_id,            # stable sort
        ))

        # Find still-tied groups and mark PENDING_TIEBREAKER
        result: list[TeamStats] = []
        i = 0
        while i < len(tied):
            j = i + 1
            while j < len(tied) and self._overall_criteria_equal(tied[i], tied[j]):
                j += 1
            sub = tied[i:j]
            if len(sub) > 1:
                for t in sub:
                    t.tiebreaker_notes.append("PENDING_TIEBREAKER:drawing_of_lots")
                    t.qualification_status = PENDING_TIEBREAKER
                    log.warning(
                        "standings_resolver: PENDING_TIEBREAKER for %s in group %s",
                        t.team_name, t.group_code,
                    )
            result.extend(sub)
            i = j

        return result

    def _overall_criteria_equal(self, a: TeamStats, b: TeamStats) -> bool:
        return (
            a.goal_difference == b.goal_difference
            and a.goals_for == b.goals_for
            and a.fair_play_score == b.fair_play_score
            and a.fifa_ranking == b.fifa_ranking
        )

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _get_fair_play_scores(self, competition_season_id: str) -> dict[str, int]:
        """Compute fair play score per team from match_events in GROUP_STAGE matches.

        FIFA penalties: Yellow=-1, Direct Red=-3, Yellow+Red (double yellow)=-4
        Returns {team_id: score} where score is negative (less negative = better).
        """
        try:
            result = await self.conn.execute(
                text("""
                    SELECT
                        mp.team_id::text,
                        SUM(CASE
                            WHEN upper(me.event_type) LIKE '%YELLOW%' AND upper(me.event_type) NOT LIKE '%RED%' THEN -1
                            WHEN upper(me.event_type) LIKE '%RED%' AND upper(me.event_type) NOT LIKE '%YELLOW%' THEN -3
                            WHEN upper(me.event_type) LIKE '%YELLOW%' AND upper(me.event_type) LIKE '%RED%' THEN -4
                            ELSE 0
                        END)::int AS fair_play_score
                    FROM match_events me
                    JOIN matches m ON m.match_id = me.match_id
                    JOIN competition_stages cs ON cs.stage_id = m.stage_id
                      AND cs.stage_type = 'GROUP_STAGE'
                    JOIN match_participants mp
                      ON mp.match_id = me.match_id
                     AND mp.team_id = me.team_id
                     AND mp.participant_role = 'TEAM'
                    WHERE m.competition_season_id = cast(:sid as uuid)
                      AND me.team_id IS NOT NULL
                    GROUP BY mp.team_id
                """),
                {"sid": competition_season_id},
            )
            return {row[0]: row[1] for row in result if row[0]}
        except Exception as exc:
            log.warning("standings_resolver: could not compute fair_play_scores: %s", exc)
            return {}

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
        """Get FINISHED GROUP_STAGE matches with FIFA ranking from teams.metadata.
        fair_play_score defaults to 0 (column may not exist in standings yet)."""
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
                    at_.display_name      AS away_name,
                    0                     AS home_fair_play_score,
                    0                     AS away_fair_play_score,
                    coalesce((ht.metadata->>'fifa_ranking')::int, 999) AS home_fifa_ranking,
                    coalesce((at_.metadata->>'fifa_ranking')::int, 999) AS away_fifa_ranking
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
                JOIN standings sh
                  ON sh.team_id = home_p.team_id
                 AND sh.competition_season_id = m.competition_season_id
                 AND sh.group_id IS NOT NULL
                JOIN competition_groups cg ON cg.group_id = sh.group_id
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
                        fair_play_score     = :fair_play_score,
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
                    "fair_play_score": team.fair_play_score,
                    "as_of": as_of,
                },
            )

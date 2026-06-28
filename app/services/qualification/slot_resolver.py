"""
TournamentSlotResolver: resolves tournament_slots from computed standings.

Resolution logic by slot.source_rank:
  1 → GROUP_WINNER   → standings position=1 in source_group_id
  2 → GROUP_RUNNER_UP → standings position=2 in source_group_id
  3 → BEST_THIRD     → from best_third ranking (may need assignment matrix)

For MATCH_WINNER slots (knockout progression):
  - Reads knockout_bracket_edges to find source match
  - Resolves winner from match_participants where score is final

When a slot resolves:
  - tournament_slots.resolved_team_id + resolved_at updated
  - match_participants.team_id updated for any SLOT participant referencing this slot
  - participant_role changed to TEAM, tournament_slot_id kept for audit
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.time import utc_now
from app.services.qualification.assignment_matrix import FIFAAssignmentMatrix
from app.services.qualification.models import SlotResolution

log = logging.getLogger(__name__)

SLOT_STATUS_RESOLVED = "RESOLVED"
SLOT_STATUS_PENDING = "PENDING"
SLOT_STATUS_PENDING_BEST_THIRD = "PENDING_BEST_THIRD_MAPPING"
SLOT_STATUS_CONFLICT = "CONFLICT"


class TournamentSlotResolver:
    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    async def resolve(self, competition_season_id: str) -> dict[str, Any]:
        """
        Resolve all unresolved tournament_slots for this competition.
        Returns summary of resolutions.
        """
        slots = await self._get_slots(competition_season_id)
        if not slots:
            return {"slots_total": 0, "resolved": 0, "pending": 0}

        # Build resolution context
        group_standings = await self._get_group_standings(competition_season_id)
        best_thirds = await self._get_ranked_best_thirds(competition_season_id)
        knockout_results = await self._get_knockout_results(competition_season_id)

        # Build assignment matrix mapping for best-third slots
        qualifying_groups = [
            t["group_code"].strip()
            for t in best_thirds
            if t["qualification_status"] == "QUALIFIED_BEST_THIRD"
        ]
        matrix_mapping: dict[str, str] = {}
        if len(qualifying_groups) == 8:
            matrix = FIFAAssignmentMatrix(self.conn)
            matrix_mapping = await matrix.resolve(competition_season_id, qualifying_groups)

        # Fallback: compute assignment via constraint propagation when DB matrix is absent.
        # Process slots in order of fewest candidates (most constrained first), so groups
        # that appear in only one slot (e.g. group L → d_e_i_j_l) are assigned first.
        if not matrix_mapping and qualifying_groups:
            matrix_mapping = self._compute_best_third_assignment(slots, best_thirds)

        resolutions: list[SlotResolution] = []
        resolved_count = 0
        pending_count = 0

        for slot in slots:
            # Skip BEST_THIRD slots that are already resolved — the draw is final.
            # Re-resolving them would overwrite the correct assignment with the
            # greedy fallback whenever fewer than 8 thirds are in the DB.
            if slot.get("slot_type") == "BEST_THIRD" and slot.get("resolved_team_id"):
                log.debug("slot %s: already resolved, skipping", slot["slot_code"])
                resolved_count += 1
                resolutions.append(
                    SlotResolution(
                        slot_id=slot["tournament_slot_id"],
                        slot_code=slot["slot_code"],
                        slot_label=slot["slot_label"],
                        resolved_team_id=slot["resolved_team_id"],
                        status=SLOT_STATUS_RESOLVED,
                        reason="ALREADY_RESOLVED",
                        source="existing",
                    )
                )
                continue

            resolution = self._resolve_slot(
                slot, group_standings, best_thirds, knockout_results, matrix_mapping
            )
            resolutions.append(resolution)

            if resolution.status == SLOT_STATUS_RESOLVED and resolution.resolved_team_id:
                await self._write_slot(competition_season_id, slot, resolution.resolved_team_id)
                await self._update_match_participants(slot, resolution.resolved_team_id)
                resolved_count += 1
            else:
                pending_count += 1

        return {
            "slots_total": len(slots),
            "resolved": resolved_count,
            "pending": pending_count,
            "resolutions": [
                {
                    "slot_code": r.slot_code,
                    "status": r.status,
                    "team_id": r.resolved_team_id,
                    "reason": r.reason,
                }
                for r in resolutions
            ],
        }

    # ── resolution logic ───────────────────────────────────────────────────────

    def _resolve_slot(
        self,
        slot: dict,
        group_standings: dict[str, list[dict]],
        best_thirds: list[dict],
        knockout_results: dict[str, dict],
        matrix_mapping: dict[str, str] | None = None,
    ) -> SlotResolution:
        slot_id = slot["tournament_slot_id"]
        slot_code = slot["slot_code"]
        slot_label = slot["slot_label"]

        source_group_id = slot.get("source_group_id")
        source_rank = slot.get("source_rank")
        source_match_id = slot.get("source_match_id")

        # ── Derive group_id from slot_code when source_group_id is missing
        # e.g. "group_b_2nd_place" → letter "b" → look up "Grupo B" in standings
        if not source_group_id and source_rank and not source_match_id:
            source_group_id = self._group_id_from_slot_code(slot_code, group_standings)
            if source_group_id:
                log.debug("slot %s: derived source_group_id from slot_code", slot_code)

        # ── Group winner / runner-up
        if source_group_id and source_rank and source_rank <= 2:
            team = self._find_by_group_rank(group_standings, source_group_id, source_rank)
            if team:
                return SlotResolution(
                    slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                    resolved_team_id=team["team_id"],
                    status=SLOT_STATUS_RESOLVED,
                    reason=f"GROUP_RANK_{source_rank}",
                    source="standings",
                )
            return SlotResolution(
                slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                resolved_team_id=None, status=SLOT_STATUS_PENDING,
                reason="group_not_completed", source="standings",
            )

        # ── Best third place slots
        if source_group_id and source_rank == 3:
            if not best_thirds:
                return SlotResolution(
                    slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                    resolved_team_id=None, status=SLOT_STATUS_PENDING,
                    reason="no_thirds_qualified_yet", source="best_thirds",
                )

            # Try matrix mapping first
            if matrix_mapping and slot_code in matrix_mapping:
                group_letter = matrix_mapping[slot_code]
                team = next(
                    (
                        t for t in best_thirds
                        if t["qualification_status"] == "QUALIFIED_BEST_THIRD"
                        and t["group_code"].strip().upper() == group_letter.upper()
                        and not t.get("slot_assigned")
                    ),
                    None,
                )
                if team:
                    team["slot_assigned"] = True
                    return SlotResolution(
                        slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                        resolved_team_id=team["team_id"],
                        status=SLOT_STATUS_RESOLVED,
                        reason=f"BEST_THIRD_MATRIX_GROUP_{group_letter}",
                        source="best_thirds",
                    )

            # If matrix_mapping covers this slot, the team wasn't available yet (PENDING)
            if matrix_mapping and slot_code in matrix_mapping:
                return SlotResolution(
                    slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                    resolved_team_id=None, status=SLOT_STATUS_PENDING_BEST_THIRD,
                    reason="matrix_group_not_yet_qualified",
                    source="best_thirds",
                )

            # Last resort: greedy fallback via allowed_groups (only when no matrix at all)
            allowed_groups = slot.get("metadata", {}).get("allowed_groups", [])
            team = self._find_best_third(best_thirds, allowed_groups)
            if team is None:
                return SlotResolution(
                    slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                    resolved_team_id=None, status=SLOT_STATUS_PENDING_BEST_THIRD,
                    reason="pending_assignment_matrix_or_groups_incomplete",
                    source="best_thirds",
                )
            return SlotResolution(
                slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                resolved_team_id=team["team_id"],
                status=SLOT_STATUS_RESOLVED,
                reason="BEST_THIRD_QUALIFIED",
                source="best_thirds",
            )

        # ── Knockout match winner/loser
        if source_match_id:
            result = knockout_results.get(source_match_id)
            if not result:
                return SlotResolution(
                    slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                    resolved_team_id=None, status=SLOT_STATUS_PENDING,
                    reason="source_match_not_finished", source="knockout_bracket",
                )
            outcome = slot.get("metadata", {}).get("outcome", "WINNER")
            team_id = result.get("winner_team_id") if outcome == "WINNER" else result.get("loser_team_id")
            if not team_id:
                return SlotResolution(
                    slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                    resolved_team_id=None, status=SLOT_STATUS_CONFLICT,
                    reason="match_finished_but_no_team_resolved", source="knockout_bracket",
                )
            return SlotResolution(
                slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
                resolved_team_id=team_id,
                status=SLOT_STATUS_RESOLVED,
                reason=f"KNOCKOUT_{outcome}",
                source="knockout_bracket",
            )

        return SlotResolution(
            slot_id=slot_id, slot_code=slot_code, slot_label=slot_label,
            resolved_team_id=None, status=SLOT_STATUS_PENDING,
            reason="no_resolution_criteria", source="unknown",
        )

    def _compute_best_third_assignment(
        self, slots: list[dict], best_thirds: list[dict]
    ) -> dict[str, str]:
        """
        Compute best-third → slot assignment via constraint propagation + backtracking.

        Returns {slot_code: group_letter}.

        Algorithm: at each step, pick the slot with fewest remaining candidates
        (most constrained first). Groups that appear in only one slot (e.g. L → d_e_i_j_l)
        are forced first, which produces the unique correct assignment.
        """
        qualified_by_group: dict[str, dict] = {
            t["group_code"].strip().upper(): t
            for t in best_thirds
            if t["qualification_status"] == "QUALIFIED_BEST_THIRD"
        }
        if not qualified_by_group:
            return {}

        best_third_slots = [s for s in slots if s.get("slot_type") == "BEST_THIRD"]

        # slot_code → eligible groups (intersection of allowed and actually qualified)
        slot_eligible: dict[str, set[str]] = {}
        for s in best_third_slots:
            allowed = {g.upper() for g in (s.get("metadata") or {}).get("allowed_groups", [])}
            slot_eligible[s["slot_code"]] = allowed & set(qualified_by_group.keys())

        assignment: dict[str, str] = {}
        assigned_groups: set[str] = set()

        # Index: group_letter → rank in best_thirds list (lower = better ranked)
        group_rank = {
            t["group_code"].strip().upper(): i
            for i, t in enumerate(best_thirds)
        }

        def backtrack(remaining: list[str]) -> bool:
            if not remaining:
                return True
            # Most constrained first
            remaining.sort(key=lambda sc: len(slot_eligible[sc] - assigned_groups))
            sc = remaining[0]
            rest = remaining[1:]
            candidates = sorted(
                slot_eligible[sc] - assigned_groups,
                key=lambda g: group_rank.get(g, 999),
            )
            if not candidates:
                return False
            for group in candidates:
                assignment[sc] = group
                assigned_groups.add(group)
                if backtrack(list(rest)):
                    return True
                del assignment[sc]
                assigned_groups.remove(group)
            return False

        backtrack(list(slot_eligible.keys()))
        log.debug("best_third_assignment: %s", assignment)
        return assignment

    def _find_by_group_rank(
        self, group_standings: dict[str, list[dict]], group_id: str, rank: int
    ) -> dict | None:
        teams = group_standings.get(group_id, [])
        for t in teams:
            if t.get("position") == rank and t.get("team_id"):
                return t
        return None

    def _group_id_from_slot_code(
        self, slot_code: str, group_standings: dict[str, list[dict]]
    ) -> str | None:
        """
        Derive group_id from slot_code pattern like 'group_b_2nd_place'.
        Extracts the letter after 'group_' and matches against group_code in standings.
        """
        import re
        m = re.match(r"^group_([a-z])_", slot_code)
        if not m:
            return None
        letter = m.group(1).upper()
        # group_standings values have group_code in each team row
        for gid, teams in group_standings.items():
            if teams:
                code = (teams[0].get("group_code") or "").strip().upper()
                if code == letter:
                    return gid
        return None

    def _find_best_third(
        self, best_thirds: list[dict], allowed_groups: list[str]
    ) -> dict | None:
        """
        Find the best-ranked third that comes from an allowed group and hasn't been
        assigned to another slot yet.

        When allowed_groups is empty, the FIFA assignment matrix hasn't been configured —
        we return None to keep the slot PENDING_BEST_THIRD_MAPPING.

        Normalizes group codes: "Grupo A" and "A" both match allowed_groups entry "A".
        """
        if not allowed_groups:
            return None
        # Normalize to uppercase letters only so "Grupo A" matches "A"
        normalized_allowed = {g.strip().upper() for g in allowed_groups}
        qualified = [
            t for t in best_thirds
            if t["qualification_status"] == "QUALIFIED_BEST_THIRD"
            and t["group_code"].strip().upper() in normalized_allowed
            and not t.get("slot_assigned")
        ]
        if not qualified:
            return None
        # Take highest-ranked (already sorted by rank asc)
        chosen = qualified[0]
        chosen["slot_assigned"] = True  # mark to avoid double-assignment in this run
        return chosen

    # ── DB writes ─────────────────────────────────────────────────────────────

    async def _write_slot(
        self, competition_season_id: str, slot: dict, team_id: str
    ) -> None:
        await self.conn.execute(
            text("""
                UPDATE tournament_slots
                SET resolved_team_id = cast(:tid as uuid),
                    resolved_at      = :now,
                    updated_at       = :now
                WHERE tournament_slot_id = cast(:slot_id as uuid)
                  AND competition_season_id = cast(:sid as uuid)
            """),
            {
                "tid": team_id,
                "now": utc_now(),
                "slot_id": slot["tournament_slot_id"],
                "sid": competition_season_id,
            },
        )

    async def _update_match_participants(self, slot: dict, team_id: str) -> None:
        """
        Promote SLOT participants that reference this slot to TEAM participants.
        Keeps tournament_slot_id for audit trail.
        """
        await self.conn.execute(
            text("""
                UPDATE match_participants
                SET team_id          = cast(:tid as uuid),
                    participant_role  = 'TEAM',
                    updated_at        = :now
                WHERE tournament_slot_id = cast(:slot_id as uuid)
                  AND (team_id IS NULL OR team_id != cast(:tid as uuid))
            """),
            {
                "tid": team_id,
                "now": utc_now(),
                "slot_id": slot["tournament_slot_id"],
            },
        )

    # ── DB reads ──────────────────────────────────────────────────────────────

    async def _get_slots(self, competition_season_id: str) -> list[dict]:
        result = await self.conn.execute(
            text("""
                SELECT
                    ts.tournament_slot_id::text,
                    ts.slot_code,
                    ts.slot_label,
                    ts.slot_type,
                    ts.source_group_id::text,
                    ts.source_match_id::text,
                    ts.source_rank,
                    ts.resolved_team_id::text,
                    ts.metadata
                FROM tournament_slots ts
                WHERE ts.competition_season_id = cast(:sid as uuid)
                ORDER BY ts.slot_code
            """),
            {"sid": competition_season_id},
        )
        return [dict(r._mapping) for r in result]

    async def _get_group_standings(
        self, competition_season_id: str
    ) -> dict[str, list[dict]]:
        """Returns {group_id: [sorted team dicts]} with position and team_id."""
        result = await self.conn.execute(
            text("""
                SELECT DISTINCT ON (s.group_id, s.team_id)
                    s.group_id::text,
                    s.team_id::text,
                    s.position,
                    s.points,
                    s.goal_difference,
                    s.goals_for,
                    s.qualification_status,
                    cg.group_code
                FROM standings s
                JOIN competition_groups cg ON cg.group_id = s.group_id
                WHERE s.competition_season_id = cast(:sid as uuid)
                  AND s.group_id IS NOT NULL
                ORDER BY s.group_id, s.team_id, s.as_of DESC NULLS LAST
            """),
            {"sid": competition_season_id},
        )
        groups: dict[str, list[dict]] = {}
        for r in result:
            gid = r.group_id
            groups.setdefault(gid, []).append(dict(r._mapping))
        # Sort each group by position
        for gid in groups:
            groups[gid].sort(key=lambda t: t.get("position") or 99)
        return groups

    async def _get_ranked_best_thirds(self, competition_season_id: str) -> list[dict]:
        result = await self.conn.execute(
            text("""
                SELECT DISTINCT ON (s.group_id, s.team_id)
                    s.team_id::text,
                    cg.group_code,
                    s.points,
                    s.goal_difference,
                    s.goals_for,
                    s.qualification_status
                FROM standings s
                JOIN competition_groups cg ON cg.group_id = s.group_id
                WHERE s.competition_season_id = cast(:sid as uuid)
                  AND s.qualification_status IN ('QUALIFIED_BEST_THIRD', 'THIRD_PLACE_CANDIDATE')
                  AND s.group_id IS NOT NULL
                ORDER BY s.group_id, s.team_id, s.as_of DESC NULLS LAST
            """),
            {"sid": competition_season_id},
        )
        thirds = [dict(r._mapping) for r in result]
        # Sort by qualification first, then by ranking criteria
        thirds.sort(key=lambda t: (
            0 if t["qualification_status"] == "QUALIFIED_BEST_THIRD" else 1,
            -(t["points"] or 0),
            -(t["goal_difference"] or 0),
            -(t["goals_for"] or 0),
        ))
        return thirds

    async def _get_knockout_results(
        self, competition_season_id: str
    ) -> dict[str, dict]:
        """Returns {match_id: {winner_team_id, loser_team_id}} for FINISHED knockout matches."""
        result = await self.conn.execute(
            text("""
                SELECT
                    m.match_id::text,
                    home_p.team_id::text AS home_team_id,
                    away_p.team_id::text AS away_team_id,
                    m.home_score,
                    m.away_score,
                    m.winner_team_id::text AS match_winner_team_id
                FROM matches m
                JOIN competition_stages cs ON cs.stage_id = m.stage_id
                JOIN match_participants home_p
                  ON home_p.match_id = m.match_id AND home_p.side = 'HOME'
                 AND home_p.participant_role = 'TEAM'
                JOIN match_participants away_p
                  ON away_p.match_id = m.match_id AND away_p.side = 'AWAY'
                 AND away_p.participant_role = 'TEAM'
                WHERE m.competition_season_id = cast(:sid as uuid)
                  AND m.status = 'FINISHED'
                  AND cs.stage_type != 'GROUP_STAGE'
            """),
            {"sid": competition_season_id},
        )
        results: dict[str, dict] = {}
        for r in result:
            home_score = r.home_score or 0
            away_score = r.away_score or 0
            if home_score > away_score:
                winner, loser = r.home_team_id, r.away_team_id
            elif away_score > home_score:
                winner, loser = r.away_team_id, r.home_team_id
            elif r.match_winner_team_id:
                # Draw after regulation — penalties winner stored in matches.winner_team_id
                winner = r.match_winner_team_id
                loser = r.away_team_id if winner == r.home_team_id else r.home_team_id
            else:
                winner = loser = None
            results[r.match_id] = {"winner_team_id": winner, "loser_team_id": loser}
        return results

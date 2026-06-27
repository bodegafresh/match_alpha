"""
FIFAAssignmentMatrix: resolves which best-third team goes to which Round of 32 slot.

The matrix is stored in competition_stages.rules['best_third_assignment_matrix']
for the ROUND_OF_32 stage. Format:

{
  "A,B,C,D,E,F,G,H": {
    "third_place_group_a_b_c_d_f": "A",
    "third_place_group_e_h_i_j_k": "E",
    ...
  },
  ...
}

Key: sorted comma-joined uppercase group letters of the 8 qualifying third-place teams.
Value: dict mapping slot_code → group_letter of the third-place team to assign.

If the combination is not found, returns {} (slots stay PENDING).
"""
from __future__ import annotations
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)


class FIFAAssignmentMatrix:
    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    async def resolve(
        self,
        competition_season_id: str,
        qualifying_groups: list[str],
    ) -> dict[str, str]:
        """
        Returns {slot_code: group_letter} for each best-third slot.
        qualifying_groups: list of uppercase group letters e.g. ["A","B","C","D","E","F","G","H"]
        Returns empty dict if matrix not configured or combination not found.
        """
        matrix = await self._load_matrix(competition_season_id)
        if not matrix:
            return {}

        key = ",".join(sorted(g.upper() for g in qualifying_groups))
        mapping = matrix.get(key)
        if not mapping:
            log.warning(
                "assignment_matrix: no entry for combination %s (competition_season_id=%s)",
                key, competition_season_id,
            )
            return {}
        return mapping

    async def _load_matrix(self, competition_season_id: str) -> dict:
        result = await self.conn.execute(
            text("""
                SELECT rules->'best_third_assignment_matrix' AS matrix
                FROM competition_stages
                WHERE competition_season_id = cast(:sid as uuid)
                  AND stage_code = 'ROUND_OF_32'
                LIMIT 1
            """),
            {"sid": competition_season_id},
        )
        row = result.fetchone()
        if not row or not row[0]:
            return {}
        return row[0] if isinstance(row[0], dict) else {}

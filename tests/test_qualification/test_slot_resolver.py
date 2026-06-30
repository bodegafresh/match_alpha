from __future__ import annotations

from collections.abc import Sequence

from app.services.qualification.slot_resolver import (
    SLOT_STATUS_PENDING_BEST_THIRD,
    TournamentSlotResolver,
)


def test_best_third_slot_not_in_active_matrix_stays_pending() -> None:
    resolver = TournamentSlotResolver(conn=None)  # type: ignore[arg-type]

    slot = {
        "tournament_slot_id": "slot-1",
        "slot_code": "third_place_group_c_e_f_h_i",
        "slot_label": "Best third C/E/F/H/I",
        "source_group_id": "group-id",
        "source_rank": 3,
        "source_match_id": None,
        "metadata": {"allowed_groups": ["C", "E", "F", "H", "I"]},
    }
    best_thirds = [
        {
            "team_id": "team-i",
            "group_code": "I",
            "qualification_status": "QUALIFIED_BEST_THIRD",
            "points": 4,
            "goal_difference": 1,
            "goals_for": 3,
        }
    ]
    matrix_mapping = {
        # Active combination resolves other slots only.
        "third_place_group_a_b_c_d_f": "D",
        "third_place_group_a_e_h_i_j": "I",
        "third_place_group_b_e_f_i_j": "B",
        "third_place_group_c_d_f_g_h": "F",
        "third_place_group_e_f_g_i_j": "J",
        "third_place_group_e_h_i_j_k": "K",
    }

    resolution = resolver._resolve_slot(
        slot=slot,
        group_standings={},
        best_thirds=best_thirds,
        knockout_results={},
        matrix_mapping=matrix_mapping,
    )

    assert resolution.status == SLOT_STATUS_PENDING_BEST_THIRD
    assert resolution.resolved_team_id is None
    assert resolution.reason == "slot_not_active_for_current_combination"


def test_slot_stage_and_rank_parses_round_of_32() -> None:
    parsed = TournamentSlotResolver._slot_stage_and_rank("round_of_32_4_winner")
    assert parsed == ("ROUND_OF_32", 4)


def test_slot_stage_and_rank_parses_quarterfinal_alias() -> None:
    parsed = TournamentSlotResolver._slot_stage_and_rank("quarterfinal_2_winner")
    assert parsed == ("QUARTER_FINAL", 2)


def test_slot_stage_and_rank_returns_none_for_non_knockout_slot() -> None:
    parsed = TournamentSlotResolver._slot_stage_and_rank("group_b_winner")
    assert parsed is None


class _FakeResult:
    def __init__(self, rows: Sequence[dict]) -> None:
        self._rows = rows

    def __iter__(self):
        for row in self._rows:
            yield type("Row", (), row)


class _FakeConnForSourceMap:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _query, _params):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return _FakeResult(
                [
                    {
                        "slot_code": "round_of_32_4_winner",
                        "source_match_id": "match-from-edges",
                    }
                ]
            )
        return _FakeResult([])


def test_build_slot_source_match_map_prefers_edges_over_fallback() -> None:
    resolver = TournamentSlotResolver(conn=_FakeConnForSourceMap())  # type: ignore[arg-type]
    slots = [
        {
            "slot_type": "WINNER",
            "slot_code": "round_of_32_4_winner",
        }
    ]

    import asyncio

    mapping = asyncio.get_event_loop().run_until_complete(
        resolver._build_slot_source_match_map("season-1", slots)
    )

    assert mapping["round_of_32_4_winner"] == "match-from-edges"

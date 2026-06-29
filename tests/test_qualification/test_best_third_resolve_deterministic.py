from __future__ import annotations

import asyncio

from app.services.qualification.best_third_resolver import BestThirdPlaceResolver
from app.services.qualification.models import BestThirdEntry, QUALIFIED_BEST_THIRD, ELIMINATED


class ResolverDeterministic(BestThirdPlaceResolver):
    def __init__(self, thirds: list[BestThirdEntry]):
        self.conn = None  # type: ignore[arg-type]
        self._thirds = thirds

    async def _get_group_stage_rules(self, competition_season_id: str) -> dict:
        return {"qualifies": {"best_third_places": 8}}

    async def _get_third_place_candidates(self, competition_season_id: str) -> list[BestThirdEntry]:
        return self._thirds

    async def _update_standings(self, competition_season_id: str, ranked: list[BestThirdEntry]) -> None:
        return None


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_entry(team_id: str, group_code: str) -> BestThirdEntry:
    return BestThirdEntry(
        team_id=team_id,
        team_name=f"Team {team_id}",
        group_code=group_code,
        points=4,
        goal_difference=1,
        goals_for=3,
        fair_play_score=0,
        fifa_ranking=999,
    )


def test_resolve_promotes_top_8_even_if_all_tied() -> None:
    thirds = [
        make_entry("A", "A"),
        make_entry("B", "B"),
        make_entry("C", "C"),
        make_entry("D", "D"),
        make_entry("E", "E"),
        make_entry("F", "F"),
        make_entry("G", "G"),
        make_entry("H", "H"),
        make_entry("I", "I"),
        make_entry("J", "J"),
        make_entry("K", "K"),
        make_entry("L", "L"),
    ]
    resolver = ResolverDeterministic(thirds)

    result = run(resolver.resolve("season-1"))

    assert result["qualified"] == 8
    assert result["eliminated"] == 4
    assert result["pending"] == 12

    qualified_statuses = [r for r in result["ranked"] if r["qualification_status"] == QUALIFIED_BEST_THIRD]
    eliminated_statuses = [r for r in result["ranked"] if r["qualification_status"] == ELIMINATED]

    assert len(qualified_statuses) == 8
    assert len(eliminated_statuses) == 4

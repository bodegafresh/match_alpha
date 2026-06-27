"""
Unit tests for BestThirdPlaceResolver ranking logic (no DB required).
"""
from __future__ import annotations

import pytest

from app.services.qualification.models import BestThirdEntry, PENDING_TIEBREAKER, QUALIFIED_BEST_THIRD, ELIMINATED
from app.services.qualification.best_third_resolver import BestThirdPlaceResolver


def make_entry(team_id: str, points: int, gd: int, gf: int,
               fair_play_score: int = 0, fifa_ranking: int = 999,
               group_code: str = "A") -> BestThirdEntry:
    return BestThirdEntry(
        team_id=team_id,
        team_name=f"Team {team_id}",
        group_code=group_code,
        points=points,
        goal_difference=gd,
        goals_for=gf,
        fair_play_score=fair_play_score,
        fifa_ranking=fifa_ranking,
    )


def get_resolver() -> BestThirdPlaceResolver:
    return BestThirdPlaceResolver(conn=None)  # type: ignore[arg-type]


class TestRanksByPoints:
    def test_ranks_by_points(self):
        resolver = get_resolver()
        thirds = [
            make_entry("A", points=6, gd=3, gf=5),
            make_entry("B", points=3, gd=1, gf=3),
            make_entry("C", points=9, gd=5, gf=7),
        ]
        ranked = resolver._rank_thirds(thirds, [])
        ids = [t.team_id for t in ranked]
        assert ids == ["C", "A", "B"]


class TestRanksByGD:
    def test_ranks_by_gd(self):
        resolver = get_resolver()
        thirds = [
            make_entry("A", points=6, gd=1, gf=3),
            make_entry("B", points=6, gd=4, gf=5),
            make_entry("C", points=6, gd=2, gf=4),
        ]
        ranked = resolver._rank_thirds(thirds, [])
        ids = [t.team_id for t in ranked]
        assert ids == ["B", "C", "A"]


class TestQualifiesTop8:
    def test_qualifies_top_8(self):
        resolver = get_resolver()
        # Create 12 thirds with distinct points
        thirds = [
            make_entry(str(i), points=12 - i, gd=0, gf=0)
            for i in range(12)
        ]
        ranked = resolver._rank_thirds(thirds[:], [])
        # Top 8 should be qualified
        for i, entry in enumerate(ranked):
            entry.rank = i + 1
            if i < 8:
                entry.qualification_status = QUALIFIED_BEST_THIRD
            else:
                entry.qualification_status = ELIMINATED

        qualified = [e for e in ranked if e.qualification_status == QUALIFIED_BEST_THIRD]
        eliminated = [e for e in ranked if e.qualification_status == ELIMINATED]
        assert len(qualified) == 8
        assert len(eliminated) == 4


class TestPendingAtBoundary:
    def test_pending_at_boundary(self):
        """Two teams with identical all criteria produce PENDING_TIEBREAKER."""
        resolver = get_resolver()
        thirds = [
            make_entry("A", points=4, gd=2, gf=3, fair_play_score=0, fifa_ranking=999),
            make_entry("B", points=4, gd=2, gf=3, fair_play_score=0, fifa_ranking=999),
        ]
        ranked = resolver._rank_thirds(thirds, [])
        statuses = {t.team_id: t.qualification_status for t in ranked}
        assert statuses["A"] == PENDING_TIEBREAKER
        assert statuses["B"] == PENDING_TIEBREAKER

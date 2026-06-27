"""
Unit tests for StandingsResolver ranking logic (no DB required).

We test the pure ranking functions by constructing TeamStats objects
and a match_index dict, then calling the internal methods directly.
"""
from __future__ import annotations

import pytest

from app.services.qualification.models import TeamStats, PENDING_TIEBREAKER
from app.services.qualification.standings_resolver import StandingsResolver


def make_team(team_id: str, points: int, gf: int, ga: int,
              fair_play_score: int = 0, fifa_ranking: int = 999) -> TeamStats:
    t = TeamStats(
        team_id=team_id,
        team_name=f"Team {team_id}",
        group_id="grp1",
        group_code="A",
        points=points,
        goals_for=gf,
        goals_against=ga,
        fair_play_score=fair_play_score,
        fifa_ranking=fifa_ranking,
    )
    return t


def make_match(home_id: str, away_id: str, home_score: int, away_score: int) -> dict:
    return {
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
    }


def get_resolver() -> StandingsResolver:
    """Create resolver without a DB connection (we only test pure methods)."""
    return StandingsResolver(conn=None)  # type: ignore[arg-type]


class TestH2HTwoWayTie:
    """2 teams tied on overall points/GD/GF; H2H shows team A beat team B."""

    def test_h2h_two_way_tie(self):
        # A and B: both 3pts, same GF/GA overall (3-2 each)
        team_a = make_team("A", points=3, gf=3, ga=2)
        team_b = make_team("B", points=3, gf=3, ga=2)

        # A beat B 2-1 head to head
        match_index = {
            ("A", "B"): make_match("A", "B", 2, 1),
        }

        resolver = get_resolver()
        result = resolver._rank_teams([team_a, team_b], match_index, [])
        assert result[0].team_id == "A"
        assert result[1].team_id == "B"


class TestH2HThreeWayTie:
    """3 teams tied on points; H2H separates them."""

    def test_h2h_three_way_tie(self):
        # A, B, C all have 3 pts, same overall GD/GF
        team_a = make_team("A", points=3, gf=2, ga=1)
        team_b = make_team("B", points=3, gf=2, ga=1)
        team_c = make_team("C", points=3, gf=2, ga=1)

        # H2H: A beat B, B beat C, C beat A
        # H2H pts: A=3, B=3, C=3 — but H2H GD: A=+1, B=+1-1=0... let's be precise
        # A vs B: 2-1 → A wins (3 pts H2H)
        # B vs C: 2-1 → B wins (3 pts H2H)
        # C vs A: 2-1 → C wins (3 pts H2H)
        # H2H pts all = 3; H2H GD: A=+1-1=0, B=+1-1=0, C=+1-1=0; H2H GF: all=3
        # Falls back to overall GD then GF then fair_play then ranking

        match_index = {
            ("A", "B"): make_match("A", "B", 2, 1),
            ("B", "C"): make_match("B", "C", 2, 1),
            ("C", "A"): make_match("C", "A", 2, 1),
        }

        resolver = get_resolver()
        # Give A a better overall GD to differentiate
        team_a.goals_for = 5
        team_b.goals_for = 4
        team_c.goals_for = 3

        result = resolver._rank_teams([team_a, team_b, team_c], match_index, [])
        # After H2H all equal, fallback to overall GF: A > B > C
        assert result[0].team_id == "A"
        assert result[1].team_id == "B"
        assert result[2].team_id == "C"


class TestOverallGDFallback:
    """H2H equal; overall GD separates."""

    def test_overall_gd_fallback(self):
        # A and B both have H2H draw 1-1
        team_a = make_team("A", points=4, gf=5, ga=2)  # GD +3
        team_b = make_team("B", points=4, gf=4, ga=3)  # GD +1

        match_index = {
            ("A", "B"): make_match("A", "B", 1, 1),
        }

        resolver = get_resolver()
        result = resolver._rank_teams([team_a, team_b], match_index, [])
        assert result[0].team_id == "A"
        assert result[1].team_id == "B"


class TestFairPlayFallback:
    """All criteria equal except fair_play_score; less negative = better."""

    def test_fair_play_fallback(self):
        team_a = make_team("A", points=6, gf=4, ga=2, fair_play_score=-1)
        team_b = make_team("B", points=6, gf=4, ga=2, fair_play_score=-3)

        # H2H draw
        match_index = {
            ("A", "B"): make_match("A", "B", 1, 1),
        }

        resolver = get_resolver()
        result = resolver._rank_teams([team_a, team_b], match_index, [])
        # -1 > -3 so A ranks first
        assert result[0].team_id == "A"
        assert result[1].team_id == "B"


class TestFIFARankingFallback:
    """All criteria equal including fair play; FIFA ranking separates (lower is better)."""

    def test_fifa_ranking_fallback(self):
        team_a = make_team("A", points=6, gf=4, ga=2, fair_play_score=0, fifa_ranking=50)
        team_b = make_team("B", points=6, gf=4, ga=2, fair_play_score=0, fifa_ranking=80)

        match_index = {
            ("A", "B"): make_match("A", "B", 1, 1),
        }

        resolver = get_resolver()
        result = resolver._rank_teams([team_a, team_b], match_index, [])
        # Ranking 50 < 80 so A ranks first
        assert result[0].team_id == "A"
        assert result[1].team_id == "B"


class TestPendingTiebreaker:
    """All criteria equal → both flagged PENDING_TIEBREAKER."""

    def test_pending_tiebreaker(self):
        team_a = make_team("A", points=6, gf=4, ga=2, fair_play_score=0, fifa_ranking=999)
        team_b = make_team("B", points=6, gf=4, ga=2, fair_play_score=0, fifa_ranking=999)

        # H2H draw
        match_index = {
            ("A", "B"): make_match("A", "B", 1, 1),
        }

        resolver = get_resolver()
        result = resolver._rank_teams([team_a, team_b], match_index, [])

        statuses = {t.team_id: t.qualification_status for t in result}
        assert statuses["A"] == PENDING_TIEBREAKER
        assert statuses["B"] == PENDING_TIEBREAKER

        notes_a = " ".join(result[0].tiebreaker_notes)
        notes_b = " ".join(result[1].tiebreaker_notes)
        assert "PENDING_TIEBREAKER" in notes_a
        assert "PENDING_TIEBREAKER" in notes_b

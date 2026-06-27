"""
Unit tests for FIFAAssignmentMatrix.resolve() logic.

We bypass the DB by subclassing and overriding _load_matrix.
"""
from __future__ import annotations

import pytest
import asyncio

from app.services.qualification.assignment_matrix import FIFAAssignmentMatrix


SAMPLE_MATRIX = {
    "A,B,C,D,E,F,G,H": {
        "third_place_group_a_b_c_d": "A",
        "third_place_group_e_f_g_h": "E",
    },
    "A,B,C,D,E,F,G,I": {
        "third_place_group_a_b_c_d": "B",
        "third_place_group_e_f_g_i": "F",
    },
}


class MockFIFAAssignmentMatrix(FIFAAssignmentMatrix):
    def __init__(self, matrix: dict):
        self._matrix = matrix
        self.conn = None  # type: ignore

    async def _load_matrix(self, competition_season_id: str) -> dict:
        return self._matrix


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestResolveKnownCombination:
    def test_known_combination_returns_mapping(self):
        resolver = MockFIFAAssignmentMatrix(SAMPLE_MATRIX)
        result = run(resolver.resolve("season-1", ["A", "B", "C", "D", "E", "F", "G", "H"]))
        assert result == {
            "third_place_group_a_b_c_d": "A",
            "third_place_group_e_f_g_h": "E",
        }

    def test_order_independent(self):
        """Groups passed in any order should produce the same result."""
        resolver = MockFIFAAssignmentMatrix(SAMPLE_MATRIX)
        result = run(resolver.resolve("season-1", ["H", "G", "F", "E", "D", "C", "B", "A"]))
        assert result == {
            "third_place_group_a_b_c_d": "A",
            "third_place_group_e_f_g_h": "E",
        }

    def test_different_combination(self):
        resolver = MockFIFAAssignmentMatrix(SAMPLE_MATRIX)
        result = run(resolver.resolve("season-1", ["A", "B", "C", "D", "E", "F", "G", "I"]))
        assert result["third_place_group_a_b_c_d"] == "B"

    def test_unknown_combination_returns_empty(self):
        resolver = MockFIFAAssignmentMatrix(SAMPLE_MATRIX)
        result = run(resolver.resolve("season-1", ["X", "Y", "Z", "W", "V", "U", "T", "S"]))
        assert result == {}

    def test_empty_matrix_returns_empty(self):
        resolver = MockFIFAAssignmentMatrix({})
        result = run(resolver.resolve("season-1", ["A", "B", "C", "D", "E", "F", "G", "H"]))
        assert result == {}

    def test_lowercase_groups_normalized(self):
        """Group letters passed in lowercase should still match."""
        resolver = MockFIFAAssignmentMatrix(SAMPLE_MATRIX)
        result = run(resolver.resolve("season-1", ["a", "b", "c", "d", "e", "f", "g", "h"]))
        assert result == {
            "third_place_group_a_b_c_d": "A",
            "third_place_group_e_f_g_h": "E",
        }

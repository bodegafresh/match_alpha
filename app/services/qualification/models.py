from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Qualification status values — mirrors DB check constraint in migration 019
QUALIFIED_GROUP_WINNER = "QUALIFIED_GROUP_WINNER"
QUALIFIED_GROUP_RUNNER_UP = "QUALIFIED_GROUP_RUNNER_UP"
THIRD_PLACE_CANDIDATE = "THIRD_PLACE_CANDIDATE"
QUALIFIED_BEST_THIRD = "QUALIFIED_BEST_THIRD"
ELIMINATED = "ELIMINATED"
PENDING = "PENDING"
PENDING_TIEBREAKER = "PENDING_TIEBREAKER"


@dataclass
class TeamStats:
    team_id: str
    team_name: str
    group_id: str
    group_code: str
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0
    points: int = 0
    position: int = 0
    qualification_status: str = PENDING
    tiebreaker_notes: list[str] = field(default_factory=list)
    fair_play_score: int = 0      # sum of card penalties; 0 if unknown
    fifa_ranking: int = 999       # lower = better; 999 = unknown

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against

    def sort_key(self) -> tuple:
        return (-self.points, -self.goal_difference, -self.goals_for)


@dataclass
class BestThirdEntry:
    team_id: str
    team_name: str
    group_code: str
    points: int
    goal_difference: int
    goals_for: int
    rank: int = 0
    qualification_status: str = PENDING
    fair_play_score: int = 0
    fifa_ranking: int = 999


@dataclass
class SlotResolution:
    slot_id: str
    slot_code: str
    slot_label: str
    resolved_team_id: str | None
    status: str  # RESOLVED | PENDING | PENDING_BEST_THIRD_MAPPING | CONFLICT
    reason: str
    source: str


@dataclass
class QualificationResult:
    competition_season_id: str
    groups_processed: int = 0
    slots_resolved: int = 0
    slots_pending: int = 0
    thirds_qualified: int = 0
    tiebreakers_pending: int = 0
    events_written: int = 0
    errors: list[str] = field(default_factory=list)
    slot_resolutions: list[SlotResolution] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "competition_season_id": self.competition_season_id,
            "groups_processed": self.groups_processed,
            "slots_resolved": self.slots_resolved,
            "slots_pending": self.slots_pending,
            "thirds_qualified": self.thirds_qualified,
            "tiebreakers_pending": self.tiebreakers_pending,
            "events_written": self.events_written,
            "errors": self.errors,
        }

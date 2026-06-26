from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NavigationItem(BaseModel):
    key: str
    label: str
    enabled: bool = True
    order: int


class CompetitionSummary(BaseModel):
    competition_id: str
    slug: str
    display_name: str
    competition_type: str | None = None


class SeasonSummary(BaseModel):
    competition_season_id: str
    slug: str
    season_label: str | None = None
    status: str | None = None
    timezone_name: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    format_code: str


class GroupLayout(BaseModel):
    group_id: str
    group_code: str
    group_name: str
    group_label: str
    group_order: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TournamentSlotLayout(BaseModel):
    tournament_slot_id: str
    slot_code: str
    slot_label: str
    slot_type: str
    source_group_id: str | None = None
    source_match_id: str | None = None
    source_rank: int | None = None
    resolved_team_id: str | None = None
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StageLayout(BaseModel):
    stage_id: str
    stage_code: str
    stage_label: str
    stage_name: str
    stage_type: str
    stage_order: int
    display_order: int
    view_type: str
    has_groups: bool
    has_slots: bool
    match_count: int
    expected_match_count: int | None = None
    groups: list[GroupLayout] = Field(default_factory=list)
    slots: list[TournamentSlotLayout] = Field(default_factory=list)
    rules: dict[str, Any] = Field(default_factory=dict)


class CompetitionCapabilities(BaseModel):
    has_groups: bool
    has_league_table: bool
    has_knockout: bool
    has_standings: bool
    has_teams: bool


class CompetitionUi(BaseModel):
    default_view: str
    navigation: list[NavigationItem]


class CompetitionLayout(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    competition: CompetitionSummary
    season: SeasonSummary
    competition_season_id: str
    name: str
    competition_type: str | None = None
    format_code: str
    navigation: list[NavigationItem]
    capabilities: CompetitionCapabilities
    ui: CompetitionUi
    stages: list[StageLayout]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompetitionLayoutEnvelope(BaseModel):
    ok: bool
    data: CompetitionLayout

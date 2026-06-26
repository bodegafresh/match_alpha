from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StageConfig:
    stage_code: str
    stage_name: str
    stage_order: int
    stage_type: str
    rules: dict[str, Any]


@dataclass(frozen=True)
class GroupConfig:
    group_code: str
    group_name: str
    group_order: int


@dataclass(frozen=True)
class SourceConfig:
    primary: str
    secondary: list[str] = field(default_factory=list)
    external_ids: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CompetitionCatalogEntry:
    slug: str
    competition_slug: str
    name: str
    competition_type: str
    domain_type: str
    format_code: str
    season_label: str
    country_code: str | None
    region: str
    confederation: str | None
    tier: int
    is_international: bool
    starts_at: str | None
    ends_at: str | None
    timezone_name: str
    source: SourceConfig
    ui_navigation: list[str]
    default_view: str
    stages: list[StageConfig]
    groups: list[GroupConfig] = field(default_factory=list)

    @property
    def competition_metadata(self) -> dict[str, Any]:
        return {
            "domain_type": self.domain_type,
            "confederation": self.confederation,
            "catalog_slug": self.slug,
            "supported_sources": [self.source.primary, *self.source.secondary],
        }

    @property
    def season_metadata(self) -> dict[str, Any]:
        return {
            "ui": {
                "navigation": self.ui_navigation,
                "default_view": self.default_view,
            },
            "format": format_metadata(self.format_code),
            "sources": {
                "primary": self.source.primary,
                "secondary": self.source.secondary,
                "priority": [self.source.primary, *self.source.secondary],
                "external_ids": self.source.external_ids,
                "capabilities": self.source.capabilities,
                "conflict_resolution": {
                    "identity": "canonical_internal_id_wins",
                    "fixtures": "primary_source_wins_unless_manual_override",
                    "results": "official_result_source_wins",
                    "stats": "source_specific_stats_do_not_merge_without_mapping",
                    "odds": "append_only_snapshots_no_overwrite",
                },
            },
        }


def bracket_rules(expected_matches: int, legs: int = 1) -> dict[str, Any]:
    return {
        "view_type": "BRACKET_ROUND" if legs == 1 else "TWO_LEG_TIE",
        "expected_matches": expected_matches,
        "legs": legs,
        "single_leg": legs == 1,
        "aggregate_score": legs == 2,
        "away_goals_rule": False,
        "extra_time": True,
        "penalties": True,
    }


GROUP_STAGE_RULES = {
    "view_type": "GROUP_TABLES",
    "expected_matches": 72,
    "teams_per_group": 4,
    "group_count": 12,
    "qualifies": {"top_n_per_group": 2, "best_third_places": 8},
    "tie_breakers": ["points", "goal_difference", "goals_for", "head_to_head", "fair_play", "draw"],
}

SINGLE_TABLE_RULES = {
    "view_type": "LEAGUE_TABLE",
    "rounds": "DOUBLE_ROUND_ROBIN",
    "tie_breakers": ["points", "goal_difference", "goals_for", "wins"],
}

LEAGUE_PHASE_RULES = {
    "view_type": "LEAGUE_PHASE_TABLE",
    "format": "SWISS_OR_LEAGUE_PHASE",
    "tie_breakers": ["points", "goal_difference", "goals_for", "away_goals", "wins"],
    "qualification": {
        "top_8": "ROUND_OF_16",
        "positions_9_to_24": "KNOCKOUT_PLAYOFF",
        "positions_25_plus": "ELIMINATED",
    },
}


def groups_a_to_l() -> list[GroupConfig]:
    return [
        GroupConfig(group_code=f"Grupo {letter}", group_name=f"Grupo {letter}", group_order=index + 1)
        for index, letter in enumerate("ABCDEFGHIJKL")
    ]


def wc2026_stages() -> list[StageConfig]:
    return [
        StageConfig("GROUP_STAGE", "Fase de grupos", 1, "GROUP_STAGE", GROUP_STAGE_RULES),
        StageConfig("ROUND_OF_32", "Dieciseisavos de final", 2, "KNOCKOUT", bracket_rules(16)),
        StageConfig("ROUND_OF_16", "Octavos de final", 3, "KNOCKOUT", bracket_rules(8)),
        StageConfig("QUARTER_FINAL", "Cuartos de final", 4, "KNOCKOUT", bracket_rules(4)),
        StageConfig("SEMI_FINAL", "Semifinal", 5, "KNOCKOUT", bracket_rules(2)),
        StageConfig("THIRD_PLACE", "Tercer lugar", 6, "THIRD_PLACE", bracket_rules(1)),
        StageConfig("FINAL", "Final", 7, "FINAL", bracket_rules(1)),
    ]


def league_stages() -> list[StageConfig]:
    return [StageConfig("LEAGUE_REGULAR", "Temporada regular", 1, "LEAGUE_PHASE", SINGLE_TABLE_RULES)]


def ucl_stages() -> list[StageConfig]:
    return [
        StageConfig("LEAGUE_PHASE", "Fase liga", 1, "LEAGUE_PHASE", LEAGUE_PHASE_RULES),
        StageConfig("KNOCKOUT_PLAYOFF", "Playoffs eliminatorios", 2, "PLAYOFF", bracket_rules(16, legs=2)),
        StageConfig("ROUND_OF_16", "Octavos de final", 3, "KNOCKOUT", bracket_rules(8, legs=2)),
        StageConfig("QUARTER_FINAL", "Cuartos de final", 4, "KNOCKOUT", bracket_rules(4, legs=2)),
        StageConfig("SEMI_FINAL", "Semifinal", 5, "KNOCKOUT", bracket_rules(2, legs=2)),
        StageConfig("FINAL", "Final", 6, "FINAL", bracket_rules(1)),
    ]


def libertadores_stages() -> list[StageConfig]:
    return [
        StageConfig(
            "GROUP_STAGE",
            "Fase de grupos",
            1,
            "GROUP_STAGE",
            {
                "view_type": "GROUP_TABLES",
                "teams_per_group": 4,
                "qualifies": {"top_n_per_group": 2},
                "tie_breakers": ["points", "goal_difference", "goals_for", "away_goals", "fair_play", "draw"],
            },
        ),
        StageConfig("ROUND_OF_16", "Octavos de final", 2, "KNOCKOUT", bracket_rules(8, legs=2)),
        StageConfig("QUARTER_FINAL", "Cuartos de final", 3, "KNOCKOUT", bracket_rules(4, legs=2)),
        StageConfig("SEMI_FINAL", "Semifinal", 4, "KNOCKOUT", bracket_rules(2, legs=2)),
        StageConfig("FINAL", "Final", 5, "FINAL", bracket_rules(1)),
    ]


COMPETITION_CATALOG: dict[str, CompetitionCatalogEntry] = {
    "wc2026": CompetitionCatalogEntry(
        slug="wc2026",
        competition_slug="fifa-world-cup",
        name="FIFA World Cup",
        competition_type="TOURNAMENT",
        domain_type="INTERNATIONAL_CUP",
        format_code="GROUPS_THEN_KNOCKOUT",
        season_label="2026",
        country_code=None,
        region="Global",
        confederation="FIFA",
        tier=1,
        is_international=True,
        starts_at="2026-06-11T00:00:00Z",
        ends_at="2026-07-19T23:59:59Z",
        timezone_name="UTC",
        source=SourceConfig(
            primary="ESPN",
            secondary=["SPORTMONKS", "API_FOOTBALL", "FOOTBALL_DATA"],
            external_ids={"FOOTBALL_DATA": "WC", "ESPN": "fifa.world"},
            capabilities={
                "ESPN": ["fixtures", "results", "scores"],
                "SPORTMONKS": ["fixtures", "teams", "venues", "players", "lineups", "events", "stats"],
                "API_FOOTBALL": ["fixtures", "teams", "venues", "players", "lineups", "events", "stats", "odds"],
                "FOOTBALL_DATA": ["fixtures", "results", "standings", "teams"],
            },
        ),
        ui_navigation=["matches", "standings", "teams", "bracket"],
        default_view="matches",
        stages=wc2026_stages(),
        groups=groups_a_to_l(),
    ),
    "ucl-2026-2027": CompetitionCatalogEntry(
        slug="ucl-2026-2027",
        competition_slug="uefa-champions-league",
        name="UEFA Champions League",
        competition_type="CUP",
        domain_type="CONTINENTAL_CLUB",
        format_code="LEAGUE_PHASE_THEN_KNOCKOUT",
        season_label="2026/2027",
        country_code=None,
        region="Europe",
        confederation="UEFA",
        tier=1,
        is_international=True,
        starts_at=None,
        ends_at=None,
        timezone_name="UTC",
        source=SourceConfig(
            primary="SPORTMONKS",
            secondary=["FOOTBALL_DATA", "API_FOOTBALL", "ESPN"],
            external_ids={"FOOTBALL_DATA": "CL"},
            capabilities={
                "SPORTMONKS": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats"],
                "FOOTBALL_DATA": ["fixtures", "standings", "teams"],
                "API_FOOTBALL": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events"],
                "ESPN": ["fixtures", "scores"],
            },
        ),
        ui_navigation=["matches", "league_phase", "teams", "bracket"],
        default_view="matches",
        stages=ucl_stages(),
    ),
    "premier-league-2026-2027": CompetitionCatalogEntry(
        slug="premier-league-2026-2027",
        competition_slug="premier-league",
        name="Premier League",
        competition_type="LEAGUE",
        domain_type="DOMESTIC_LEAGUE",
        format_code="SINGLE_TABLE_LEAGUE",
        season_label="2026/2027",
        country_code="GB",
        region="Europe",
        confederation="UEFA",
        tier=1,
        is_international=False,
        starts_at=None,
        ends_at=None,
        timezone_name="Europe/London",
        source=SourceConfig(
            primary="FOOTBALL_DATA",
            secondary=["SPORTMONKS", "API_FOOTBALL", "ESPN"],
            external_ids={"FOOTBALL_DATA": "PL"},
            capabilities={
                "FOOTBALL_DATA": ["fixtures", "results", "standings", "teams"],
                "SPORTMONKS": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats"],
                "API_FOOTBALL": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats", "odds"],
                "ESPN": ["fixtures", "scores"],
            },
        ),
        ui_navigation=["matches", "standings", "teams"],
        default_view="matches",
        stages=league_stages(),
    ),
    "chile-primera-2026": CompetitionCatalogEntry(
        slug="chile-primera-2026",
        competition_slug="chile-primera",
        name="Chile Primera División",
        competition_type="LEAGUE",
        domain_type="DOMESTIC_LEAGUE",
        format_code="SINGLE_TABLE_LEAGUE",
        season_label="2026",
        country_code="CL",
        region="South America",
        confederation="CONMEBOL",
        tier=1,
        is_international=False,
        starts_at=None,
        ends_at=None,
        timezone_name="America/Santiago",
        source=SourceConfig(
            primary="API_FOOTBALL",
            secondary=["SPORTMONKS", "ESPN"],
            capabilities={
                "API_FOOTBALL": ["fixtures", "standings", "teams", "venues", "players", "events", "stats", "odds"],
                "SPORTMONKS": ["fixtures", "standings", "teams", "venues", "players", "events", "stats"],
                "ESPN": ["fixtures", "scores"],
            },
        ),
        ui_navigation=["matches", "standings", "teams"],
        default_view="matches",
        stages=league_stages(),
    ),
    "libertadores-2026": CompetitionCatalogEntry(
        slug="libertadores-2026",
        competition_slug="copa-libertadores",
        name="Copa Libertadores",
        competition_type="CUP",
        domain_type="CONTINENTAL_CLUB",
        format_code="GROUPS_THEN_KNOCKOUT",
        season_label="2026",
        country_code=None,
        region="South America",
        confederation="CONMEBOL",
        tier=1,
        is_international=True,
        starts_at=None,
        ends_at=None,
        timezone_name="UTC",
        source=SourceConfig(
            primary="API_FOOTBALL",
            secondary=["SPORTMONKS", "ESPN"],
            capabilities={
                "API_FOOTBALL": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats", "odds"],
                "SPORTMONKS": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats"],
                "ESPN": ["fixtures", "scores"],
            },
        ),
        ui_navigation=["matches", "standings", "teams", "bracket"],
        default_view="matches",
        stages=libertadores_stages(),
    ),
}


def format_metadata(format_code: str) -> dict[str, Any]:
    if format_code == "GROUPS_THEN_KNOCKOUT":
        return {
            "type": format_code,
            "has_groups": True,
            "has_league_table": False,
            "has_knockout": True,
            "has_playoffs": False,
            "has_best_third_places": True,
        }
    if format_code == "LEAGUE_PHASE_THEN_KNOCKOUT":
        return {
            "type": format_code,
            "has_groups": False,
            "has_league_table": True,
            "has_knockout": True,
            "has_playoffs": True,
            "has_two_leg_ties": True,
        }
    if format_code == "SINGLE_TABLE_LEAGUE":
        return {
            "type": format_code,
            "has_groups": False,
            "has_league_table": True,
            "has_knockout": False,
            "has_playoffs": False,
        }
    return {"type": format_code}


def get_catalog_entry(slug: str) -> CompetitionCatalogEntry:
    try:
        return COMPETITION_CATALOG[slug]
    except KeyError as exc:
        raise ValueError(f"Unsupported competition season: {slug}") from exc


def supported_competitions() -> list[CompetitionCatalogEntry]:
    return list(COMPETITION_CATALOG.values())

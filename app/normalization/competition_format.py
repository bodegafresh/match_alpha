from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.competitions.catalog import CompetitionCatalogEntry, StageConfig


@dataclass(frozen=True)
class FormatPlan:
    format_code: str
    stages: list[StageConfig]
    has_groups: bool
    has_league_table: bool
    has_knockout: bool
    default_stage_code: str


class CompetitionFormatNormalizer(Protocol):
    format_code: str

    def build_plan(self, entry: CompetitionCatalogEntry) -> FormatPlan:
        ...


class GroupsThenKnockoutNormalizer:
    format_code = "GROUPS_THEN_KNOCKOUT"

    def build_plan(self, entry: CompetitionCatalogEntry) -> FormatPlan:
        return FormatPlan(
            format_code=entry.format_code,
            stages=entry.stages,
            has_groups=True,
            has_league_table=False,
            has_knockout=True,
            default_stage_code="GROUP_STAGE",
        )


class SingleTableLeagueNormalizer:
    format_code = "SINGLE_TABLE_LEAGUE"

    def build_plan(self, entry: CompetitionCatalogEntry) -> FormatPlan:
        return FormatPlan(
            format_code=entry.format_code,
            stages=entry.stages,
            has_groups=False,
            has_league_table=True,
            has_knockout=False,
            default_stage_code="LEAGUE_REGULAR",
        )


class LeaguePhaseThenKnockoutNormalizer:
    format_code = "LEAGUE_PHASE_THEN_KNOCKOUT"

    def build_plan(self, entry: CompetitionCatalogEntry) -> FormatPlan:
        return FormatPlan(
            format_code=entry.format_code,
            stages=entry.stages,
            has_groups=False,
            has_league_table=True,
            has_knockout=True,
            default_stage_code="LEAGUE_PHASE",
        )


class DomesticCupNormalizer:
    format_code = "DOMESTIC_CUP"

    def build_plan(self, entry: CompetitionCatalogEntry) -> FormatPlan:
        first_stage = entry.stages[0].stage_code if entry.stages else "ROUND_1"
        return FormatPlan(
            format_code=entry.format_code,
            stages=entry.stages,
            has_groups=False,
            has_league_table=False,
            has_knockout=True,
            default_stage_code=first_stage,
        )


class TwoLegKnockoutNormalizer:
    format_code = "TWO_LEG_KNOCKOUT"

    def build_plan(self, entry: CompetitionCatalogEntry) -> FormatPlan:
        first_stage = entry.stages[0].stage_code if entry.stages else "ROUND_1"
        return FormatPlan(
            format_code=entry.format_code,
            stages=entry.stages,
            has_groups=False,
            has_league_table=False,
            has_knockout=True,
            default_stage_code=first_stage,
        )


NORMALIZERS: dict[str, CompetitionFormatNormalizer] = {
    GroupsThenKnockoutNormalizer.format_code: GroupsThenKnockoutNormalizer(),
    SingleTableLeagueNormalizer.format_code: SingleTableLeagueNormalizer(),
    LeaguePhaseThenKnockoutNormalizer.format_code: LeaguePhaseThenKnockoutNormalizer(),
    DomesticCupNormalizer.format_code: DomesticCupNormalizer(),
    TwoLegKnockoutNormalizer.format_code: TwoLegKnockoutNormalizer(),
}


def get_format_normalizer(format_code: str) -> CompetitionFormatNormalizer:
    try:
        return NORMALIZERS[format_code]
    except KeyError as exc:
        raise ValueError(f"Unsupported competition format: {format_code}") from exc

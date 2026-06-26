from app.competitions.catalog import get_catalog_entry, supported_competitions
from app.normalization.competition_format import get_format_normalizer


def test_supported_competitions_have_format_normalizers() -> None:
    entries = supported_competitions()
    assert {entry.slug for entry in entries} >= {
        "wc2026",
        "ucl-2026-2027",
        "premier-league-2026-2027",
        "chile-primera-2026",
        "libertadores-2026",
    }
    for entry in entries:
        normalizer = get_format_normalizer(entry.format_code)
        plan = normalizer.build_plan(entry)
        assert plan.format_code == entry.format_code
        assert plan.default_stage_code


def test_wc2026_catalog_defines_groups_then_knockout_rules() -> None:
    entry = get_catalog_entry("wc2026")
    assert entry.format_code == "GROUPS_THEN_KNOCKOUT"
    assert len(entry.groups) == 12
    assert [stage.stage_code for stage in entry.stages] == [
        "GROUP_STAGE",
        "ROUND_OF_32",
        "ROUND_OF_16",
        "QUARTER_FINAL",
        "SEMI_FINAL",
        "THIRD_PLACE",
        "FINAL",
    ]
    assert entry.stages[0].rules["qualifies"]["best_third_places"] == 8


def test_league_catalogs_do_not_define_groups() -> None:
    premier = get_catalog_entry("premier-league-2026-2027")
    chile = get_catalog_entry("chile-primera-2026")
    assert premier.format_code == "SINGLE_TABLE_LEAGUE"
    assert chile.format_code == "SINGLE_TABLE_LEAGUE"
    assert premier.groups == []
    assert chile.groups == []

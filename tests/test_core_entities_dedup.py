from app.normalization.core_entities_dedup import (
    _TeamRow,
    _RefereeRow,
    _VenueRow,
    _build_referee_merge_plan,
    _build_team_merge_plan,
    _build_venue_merge_plan,
)


def test_build_team_merge_plan_groups_by_type_country_and_normalized_name() -> None:
    rows = [
        _TeamRow("t1", None, "River Plate", "river plate", "CLUB", "AR", {}, 1, 0, 0),
        _TeamRow("t2", None, "River Plate", "river plate", "CLUB", "AR", {}, 2, 0, 0),
        _TeamRow("t3", None, "River Plate", "river plate", "CLUB", "UY", {}, 3, 0, 0),
    ]
    plan = _build_team_merge_plan(rows)
    assert len(plan) == 1
    assert plan[0]["reason"] == "EXACT_NORMALIZED_TEAM"


def test_build_referee_merge_plan_groups_by_normalized_and_nationality() -> None:
    rows = [
        _RefereeRow("r1", None, "Juan Perez", "juan perez", "AR", {}, 1, 0),
        _RefereeRow("r2", None, "J. Perez", "juan perez", "AR", {}, 2, 0),
        _RefereeRow("r3", None, "Juan Perez", "juan perez", "UY", {}, 3, 0),
    ]
    plan = _build_referee_merge_plan(rows)
    assert len(plan) == 1
    assert plan[0]["reason"] == "EXACT_NORMALIZED_REFEREE"


def test_build_venue_merge_plan_groups_by_normalized_name_city_country() -> None:
    rows = [
        _VenueRow("v1", None, "Monumental", "Buenos Aires", "AR", None, None, None, {}, 1, 0),
        _VenueRow("v2", None, "Estadio Monumental", "Buenos Aires", "AR", None, None, None, {}, 2, 0),
        _VenueRow("v3", None, "Monumental", "Lima", "PE", None, None, None, {}, 3, 0),
    ]
    plan = _build_venue_merge_plan(rows)
    assert len(plan) == 0

    rows_same = [
        _VenueRow("v1", None, "Monumental", "Buenos Aires", "AR", None, None, None, {}, 1, 0),
        _VenueRow("v2", None, "Monumental", "Buenos Aires", "AR", None, None, None, {}, 2, 0),
    ]
    plan_same = _build_venue_merge_plan(rows_same)
    assert len(plan_same) == 1
    assert plan_same[0]["reason"] == "EXACT_NORMALIZED_VENUE_CITY_COUNTRY"

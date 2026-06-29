from app.normalization.core_entities_identity import _group_duplicates


def test_group_duplicates_groups_only_repeated_keys() -> None:
    keys = [
        ("river plate", "club", "AR"),
        ("river plate", "club", "AR"),
        ("river plate", "club", "UY"),
        ("monumental", "buenos aires", "AR"),
        ("monumental", "buenos aires", "AR"),
    ]
    duplicates = _group_duplicates(keys)

    assert len(duplicates) == 2
    assert duplicates[0]["count"] == 2
    assert duplicates[1]["count"] == 2

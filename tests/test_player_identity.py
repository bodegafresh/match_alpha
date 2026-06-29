from app.normalization.player_identity import (
    is_abbreviated_name,
    name_signature,
    normalize_identity_name,
    prefer_display_name,
)


def test_normalize_identity_name_removes_accents_and_symbols() -> None:
    assert normalize_identity_name("  Exequiel    Palacios ") == "exequiel palacios"
    assert normalize_identity_name("É. Palacios") == "e palacios"


def test_name_signature_for_first_initial_and_last_name() -> None:
    assert name_signature("e palacios") == ("palacios", "e")
    assert name_signature("exequiel palacios") == ("palacios", "e")


def test_is_abbreviated_name() -> None:
    assert is_abbreviated_name("e palacios") is True
    assert is_abbreviated_name("exequiel palacios") is False


def test_prefer_display_name_prefers_non_abbreviated() -> None:
    assert prefer_display_name("E. Palacios", "Exequiel Palacios") == "Exequiel Palacios"
    assert prefer_display_name("Exequiel Palacios", "E. Palacios") == "Exequiel Palacios"

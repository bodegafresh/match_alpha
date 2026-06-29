from app.normalization.team_normalizer import slugify_name


def test_team_aliases_are_canonical() -> None:
    assert slugify_name("EE.UU.") == "estados-unidos"
    assert slugify_name("United States") == "estados-unidos"
    assert slugify_name("Türkiye") == "turquia"
    assert slugify_name("Curaçao") == "curazao"
    assert slugify_name("South Africa") == "sudafrica"


def test_cape_verde_aliases_are_canonical() -> None:
    assert slugify_name("Cape Verde") == "cabo-verde"
    assert slugify_name("Cape Verde Islands") == "cabo-verde"
    assert slugify_name("Cabo Verde") == "cabo-verde"


import re
import unicodedata


ALIASES = {
    "ee-uu": "estados-unidos",
    "eeuu": "estados-unidos",
    "usa": "estados-unidos",
    "usmnt": "estados-unidos",
    "united-states": "estados-unidos",
    "estados-unidos": "estados-unidos",
    "turkiye": "turquia",
    "turkey": "turquia",
    "curacao": "curazao",
    "curaçao": "curazao",
    "south-africa": "sudafrica",
    "sudafrica": "sudafrica",
    "bosnia": "bosnia-herzegovina",
    "bosnia-and-herzegovina": "bosnia-herzegovina",
    "new-zealand": "nueva-zelanda",
    "czechia": "republica-checa",
}


def slugify_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_value = ascii_value.replace("&", " and ")
    ascii_value = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return ALIASES.get(ascii_value, ascii_value)

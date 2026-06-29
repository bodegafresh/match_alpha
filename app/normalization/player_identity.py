from __future__ import annotations

import re
import unicodedata


_CHAR_FOLDS = str.maketrans(
    {
        "ß": "ss",
        "ẞ": "ss",
        "ø": "o",
        "Ø": "o",
        "đ": "d",
        "Đ": "d",
        "ł": "l",
        "Ł": "l",
        "þ": "th",
        "Þ": "th",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
    }
)


def normalize_identity_name(name: str) -> str:
    """Normalize player/team names for identity matching across data sources."""
    s = str(name or "").translate(_CHAR_FOLDS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def name_tokens(name: str) -> list[str]:
    return [t for t in (name or "").split(" ") if t]


def name_signature(normalized_name: str) -> tuple[str, str] | None:
    tokens = name_tokens(normalized_name)
    if len(tokens) < 2:
        return None
    first = tokens[0]
    surname = " ".join(tokens[1:])
    if not first or not surname:
        return None
    return (surname, first[0])


def is_abbreviated_name(normalized_name: str) -> bool:
    tokens = name_tokens(normalized_name)
    return bool(tokens) and len(tokens[0]) == 1 and len(tokens) >= 2


def prefer_display_name(current: str, candidate: str) -> str:
    current = current or ""
    candidate = candidate or ""
    if not current:
        return candidate
    if not candidate:
        return current

    current_first = name_tokens(current.lower())[0] if name_tokens(current.lower()) else ""
    candidate_first = name_tokens(candidate.lower())[0] if name_tokens(candidate.lower()) else ""

    # Prefer non-abbreviated first names and then longer labels.
    if len(current_first) == 1 and len(candidate_first) > 1:
        return candidate
    if len(candidate) > len(current):
        return candidate
    return current

from typing import Any

from app.core.hashing import sha256_json


FEATURE_SET_VERSION = "features-v1"


def build_match_features(match: dict[str, Any]) -> dict[str, Any]:
    return {
        "is_neutral": bool(match.get("is_neutral", True)),
        "status": match.get("status"),
        "stage_id": str(match.get("stage_id") or ""),
        "group_id": str(match.get("group_id") or ""),
    }


def feature_source_hash(features: dict[str, Any]) -> str:
    return sha256_json(features)


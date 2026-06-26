from fastapi import Header, HTTPException, status

from app.core.config import get_settings


def require_internal_key(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_internal_key:
        if settings.is_production:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="job auth not configured")
        return
    expected = f"Bearer {settings.api_internal_key}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


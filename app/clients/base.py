import asyncio
from typing import Any

import httpx

from app.core.config import get_settings


class HttpClient:
    source = "UNKNOWN"

    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = get_settings().http_timeout_seconds

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                try:
                    response = await client.get(url, params=params)
                    if response.status_code == 429:
                        wait = min(int(response.headers.get("Retry-After", 60)), 120)
                        if attempt < 2:
                            await asyncio.sleep(wait)
                            continue
                        raise httpx.HTTPStatusError(
                            f"Rate limited after {attempt + 1} attempts",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    last_exc = exc
                    raise
        raise last_exc  # type: ignore[misc]

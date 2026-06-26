from typing import Any

from app.clients.base import HttpClient
from app.core.config import get_settings


class EspnClient(HttpClient):
    source = "ESPN"

    def __init__(self) -> None:
        super().__init__(get_settings().espn_base_url)

    async def scoreboard(self, date: str | None = None) -> dict[str, Any]:
        params = {"dates": date} if date else None
        return await self.get("scoreboard", params=params)

    async def summary(self, event_id: str) -> dict[str, Any]:
        return await self.get("summary", params={"event": event_id})


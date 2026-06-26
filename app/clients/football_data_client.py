from typing import Any

from app.clients.base import HttpClient
from app.core.config import get_settings


class FootballDataClient(HttpClient):
    source = "FOOTBALL_DATA"

    def __init__(self) -> None:
        settings = get_settings()
        headers = {"X-Auth-Token": settings.football_data_token} if settings.football_data_token else {}
        super().__init__("https://api.football-data.org/v4", headers=headers)

    async def competition_matches(self, code: str, season: str | int | None = None) -> dict[str, Any]:
        params = {"season": season} if season else None
        return await self.get(f"competitions/{code}/matches", params=params)

    async def competitions(self) -> dict[str, Any]:
        return await self.get("competitions")

    async def competition(self, code: str) -> dict[str, Any]:
        return await self.get(f"competitions/{code}")

    async def competition_standings(self, code: str, season: str | int | None = None) -> dict[str, Any]:
        params = {"season": season} if season else None
        return await self.get(f"competitions/{code}/standings", params=params)

    async def competition_teams(self, code: str, season: str | int | None = None) -> dict[str, Any]:
        params = {"season": season} if season else None
        return await self.get(f"competitions/{code}/teams", params=params)

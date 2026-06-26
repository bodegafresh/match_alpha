from typing import Any

from app.clients.base import HttpClient
from app.core.config import get_settings


class ApiFootballClient(HttpClient):
    source = "API_FOOTBALL"

    def __init__(self) -> None:
        settings = get_settings()
        headers = {"x-apisports-key": settings.api_football_key} if settings.api_football_key else {}
        super().__init__("https://v3.football.api-sports.io", headers=headers)

    async def leagues(
        self,
        country: str | None = None,
        season: str | int | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if country:
            params["country"] = country
        if season:
            params["season"] = season
        if search:
            params["search"] = search
        return await self.get("leagues", params=params or None)

    async def fixtures(
        self,
        league: str | int | None = None,
        season: str | int | None = None,
        date: str | None = None,
        next_count: int | None = None,
        last_count: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if league:
            params["league"] = league
        if season:
            params["season"] = season
        if date:
            params["date"] = date
        if next_count:
            params["next"] = next_count
        if last_count:
            params["last"] = last_count
        return await self.get("fixtures", params=params or None)

    async def standings(self, league: str | int, season: str | int) -> dict[str, Any]:
        return await self.get("standings", params={"league": league, "season": season})

    async def teams(self, league: str | int, season: str | int) -> dict[str, Any]:
        return await self.get("teams", params={"league": league, "season": season})

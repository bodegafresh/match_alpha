from typing import Any

from app.clients.base import HttpClient
from app.core.config import get_settings


class OddsApiClient(HttpClient):
    source = "THE_ODDS_API"

    def __init__(self) -> None:
        super().__init__("https://api.the-odds-api.com/v4")
        self.api_key = get_settings().the_odds_api_key

    async def odds(self, sport: str, regions: str = "us,eu", markets: str = "h2h") -> list[dict[str, Any]]:
        return await self.get(
            f"sports/{sport}/odds",
            params={"apiKey": self.api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
        )


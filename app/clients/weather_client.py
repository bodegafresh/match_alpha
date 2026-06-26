from typing import Any

from app.clients.base import HttpClient
from app.core.config import get_settings


class WeatherClient(HttpClient):
    source = "WEATHER"

    def __init__(self) -> None:
        super().__init__("https://api.weatherapi.com/v1")
        self.api_key = get_settings().weather_api_key

    async def forecast(self, lat: float, lon: float, dt: str) -> dict[str, Any]:
        return await self.get("forecast.json", params={"key": self.api_key, "q": f"{lat},{lon}", "dt": dt, "aqi": "no"})


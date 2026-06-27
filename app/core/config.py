from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="local", alias="APP_ENV")
    app_name: str = Field(default="match-alpha-backend", alias="APP_NAME")
    api_prefix: str = Field(default="/api/v1", alias="API_PREFIX")
    api_internal_key: str | None = Field(default=None, alias="API_INTERNAL_KEY")

    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    db_connect_timeout_seconds: int = Field(default=5, alias="DB_CONNECT_TIMEOUT_SECONDS")
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")

    frontend_allowed_origins: str = Field(
        default="http://localhost:5173,https://bodegafresh.github.io",
        alias="FRONTEND_ALLOWED_ORIGINS",
    )
    default_season_slug: str = Field(default="wc2026", alias="DEFAULT_SEASON_SLUG")
    default_timezone: str = Field(default="UTC", alias="DEFAULT_TIMEZONE")
    git_sha: str | None = Field(default=None, alias="GIT_SHA")

    espn_base_url: str = Field(
        default="https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world",
        alias="ESPN_BASE_URL",
    )
    football_data_token: str | None = Field(default=None, alias="FOOTBALL_DATA_TOKEN")
    sportmonks_api_token: str | None = Field(default=None, alias="SPORTMONKS_API_TOKEN")
    api_football_key: str | None = Field(default=None, alias="API_FOOTBALL_KEY")
    the_odds_api_key: str | None = Field(default=None, alias="THE_ODDS_API_KEY")
    weather_api_key: str | None = Field(default=None, alias="WEATHER_API_KEY")
    news_api_key: str | None = Field(default=None, alias="NEWS_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")

    job_max_retries: int = Field(default=3, alias="JOB_MAX_RETRIES")
    http_timeout_seconds: int = Field(default=30, alias="HTTP_TIMEOUT_SECONDS")
    odds_refresh_window_hours: int = Field(default=72, alias="ODDS_REFRESH_WINDOW_HOURS")
    weather_cache_ttl_minutes: int = Field(default=120, alias="WEATHER_CACHE_TTL_MINUTES")
    min_calibration_sample_size: int = Field(default=50, alias="MIN_CALIBRATION_SAMPLE_SIZE")
    max_ece_for_bettable: float = Field(default=0.08, alias="MAX_ECE_FOR_BETTABLE")

    slack_webhook_url: str | None = Field(default=None, alias="SLACK_WEBHOOK_URL")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("api_prefix")
    @classmethod
    def normalize_api_prefix(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            value = "/" + value
        return value.rstrip("/")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_allowed_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    return Settings()

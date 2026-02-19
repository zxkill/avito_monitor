from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str = Field(alias="BOT_TOKEN")

    pg_host: str = Field(default="db", alias="POSTGRES_HOST")
    pg_port: int = Field(default=5432, alias="POSTGRES_PORT")
    pg_db: str = Field(default="avito_bot", alias="POSTGRES_DB")
    pg_user: str = Field(default="avito_bot", alias="POSTGRES_USER")
    pg_password: str = Field(default="avito_bot", alias="POSTGRES_PASSWORD")

    avito_city_slug: str = Field(default="magnitogorsk", alias="AVITO_CITY_SLUG")
    avito_max_pages: int = Field(default=5, alias="AVITO_MAX_PAGES")
    avito_page_delay_s: int = Field(default=5, alias="AVITO_PAGE_DELAY_S")
    avito_timeout_s: int = Field(default=25, alias="AVITO_TIMEOUT_S")
    avito_poll_minutes: int = Field(default=30, alias="AVITO_POLL_MINUTES")
    avito_user_agent: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        alias="AVITO_USER_AGENT",
    )
    notify_chat_id: int = Field(default=0, alias="NOTIFY_CHAT_ID")
    avito_between_queries_delay_s: int = Field(default=60, alias="AVITO_BETWEEN_QUERIES_DELAY_S")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def pg_dsn(self) -> str:
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"

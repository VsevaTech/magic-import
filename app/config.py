"""Application settings (environment driven)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Magic Import"
    app_version: str = "0.1.0"
    debug: bool = False

    data_dir: Path = Field(default=Path("./data"))
    database_url: str = ""
    upload_dir: Path | None = None
    max_upload_mb: int = 20
    delete_uploads_after_processing: bool = True
    upload_retention_hours: int = 24

    # AI (optional)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ai_enabled_default: bool = False

    # Table paging
    page_size: int = 50

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'magic_import.db').as_posix()}"

    @property
    def resolved_upload_dir(self) -> Path:
        return self.upload_dir or (self.data_dir / "uploads")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def ai_available(self) -> bool:
        return bool(self.gemini_api_key)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_upload_dir.mkdir(parents=True, exist_ok=True)
    return settings

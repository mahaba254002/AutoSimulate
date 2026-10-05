"""
Central application settings, loaded from environment variables / .env.
Everything that varies between local dev, staging, and prod lives here —
nowhere else in the codebase should read os.environ directly.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env", env_file_encoding="utf-8", extra="ignore",
        populate_by_name=True,
    )
    database_url_override: str | None = Field(default=None, validation_alias="DATABASE_URL")

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "alpha_kb"
    postgres_user: str = "alpha_kb"
    postgres_password: str = "changeme"

    @property
    def database_url(self) -> str:
        return self.database_url_override or URL.create(
            "postgresql+psycopg2", username=self.postgres_user, password=self.postgres_password,
            host=self.postgres_host, port=self.postgres_port, database=self.postgres_db,
        ).render_as_string(hide_password=False)

    # --- Qdrant ---
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection: str = "alpha_embeddings"

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # --- MinIO / S3 ---
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "alpha-kb"

    # --- BRAIN API ---
    brain_api_base_url: str = "https://api.worldquantbrain.com"
    brain_email: str = ""
    brain_password: str = ""

    # --- LLM ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""

    # --- App ---
    environment: str = "development"
    log_level: str = "INFO"
    daily_simulation_budget: int = Field(default=5000, ge=1, le=5000)


@lru_cache
def get_settings() -> Settings:
    """Cached so .env is parsed once per process, not on every call site."""
    return Settings()

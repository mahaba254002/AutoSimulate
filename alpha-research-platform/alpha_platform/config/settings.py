import os
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy import URL


load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str


def get_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        database_url = URL.create(
            drivername="postgresql+psycopg",
            username=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB", "alpha_research_platform"),
        ).render_as_string(hide_password=False)

    return Settings(database_url=database_url)
"""
Central application settings, loaded from environment variables / .env.
Everything that varies between local dev, staging, and prod lives here —
nowhere else in the codebase should read os.environ directly.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "alpha_kb"
    postgres_user: str = "alpha_kb"
    postgres_password: str = "changeme"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

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

    # --- App ---
    environment: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Cached so .env is parsed once per process, not on every call site."""
    return Settings()
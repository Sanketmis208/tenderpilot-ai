"""Application configuration loaded from environment variables (pydantic-settings)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class AppMode(StrEnum):
    """Operating mode.

    demo: synthetic data allowed, deterministic analysers used when no LLM is configured,
          every such output is labelled as rule-based / simulated.
    integrated: real providers expected; missing providers raise a ServiceUnavailable state
          instead of silently falling back.
    """

    DEMO = "demo"
    INTEGRATED = "integrated"


class StorageBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TP_", extra="ignore")

    app_name: str = "TenderPilot AI"
    environment: str = "development"
    mode: AppMode = AppMode.DEMO
    debug: bool = False
    base_url: str = "http://localhost:8080"
    secret_key: SecretStr = SecretStr("dev-insecure-secret-change-me-0123456789abcdef")
    # Fernet-style key material for encrypting integration credentials (derived if unset).
    encryption_key: SecretStr | None = None

    database_url: str = "postgresql+psycopg://localhost/tenderpilot"
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"
    celery_eager: bool = False

    storage_backend: StorageBackend = StorageBackend.LOCAL
    local_storage_path: Path = BASE_DIR / "var" / "storage"
    s3_bucket: str = "tenderpilot-dev"
    s3_endpoint_url: str | None = None  # set for MinIO
    s3_region: str = "ap-south-1"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_kms_key_id: str | None = None
    presign_ttl_seconds: int = 300

    max_upload_bytes: int = 50 * 1024 * 1024
    max_zip_members: int = 200
    max_zip_uncompressed_bytes: int = 300 * 1024 * 1024

    textract_enabled: bool = False
    ocr_min_text_chars_per_page: int = 40

    # LLM / embeddings
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_fast_model: str = "claude-haiku-5-5"
    llm_timeout_seconds: float = 60.0
    llm_max_output_tokens: int = 4096
    embedding_provider: str = "hashing"  # hashing | openai_compatible
    embedding_dim: int = 384
    embedding_api_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None

    # Agent limits
    agent_max_steps: int = 25
    agent_max_tokens_per_run: int = 200_000
    agent_max_cost_usd_per_run: float = 2.0

    # Sessions & auth
    session_cookie_name: str = "tp_session"
    session_ttl_hours: int = 24 * 7
    session_idle_timeout_hours: int = 24
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = False
    email_verification_required: bool = False
    invitation_ttl_days: int = 7
    password_reset_ttl_minutes: int = 30
    rate_limit_per_minute: int = 120
    auth_rate_limit_per_minute: int = 10

    # Email
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "TenderPilot AI <no-reply@tenderpilot.local>"
    smtp_starttls: bool = True

    # URL import
    url_import_allowed_hosts: list[str] = Field(default_factory=list)
    url_import_max_bytes: int = 25 * 1024 * 1024

    cors_origins: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def llm_configured(self) -> bool:
        return self.anthropic_api_key is not None and bool(self.anthropic_api_key.get_secret_value())

    @property
    def sync_psycopg_dsn(self) -> str:
        """DSN usable by raw psycopg (LangGraph checkpointer)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()

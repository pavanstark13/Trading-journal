"""Application configuration. Every value comes from the environment."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── core ────────────────────────────────────────────────────────────────
    env: Literal["development", "test", "production"] = "development"
    api_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:3000"
    enable_docs: bool = True
    log_level: str = "INFO"

    # ── secrets ─────────────────────────────────────────────────────────────
    jwt_secret: str = "dev-only-insecure-change-me"
    master_encryption_key: str = "dev-only-insecure-change-me-32b!"

    # ── datastores ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    redis_url: str = "redis://localhost:6379/0"

    # ── auth ────────────────────────────────────────────────────────────────
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 30
    require_2fa_for_superadmin: bool = False
    login_max_attempts: int = 5
    login_lockout_threshold: int = 10

    # ── EA ──────────────────────────────────────────────────────────────────
    ea_timestamp_skew_sec: int = 120
    ea_nonce_ttl_sec: int = 300
    ea_event_batch_max: int = 100
    member_poll_wait_sec: int = 25
    member_heartbeat_timeout_sec: int = 90
    master_heartbeat_timeout_sec: int = 120

    # ── copy engine ─────────────────────────────────────────────────────────
    default_max_signal_age_sec: int = 60
    copy_lease_ttl_sec: int = 45

    # ── telegram ────────────────────────────────────────────────────────────
    telegram_api_base: str = "https://api.telegram.org"
    telegram_max_retries: int = 6
    telegram_rate_limit_per_sec: int = 20

    # ── observability ───────────────────────────────────────────────────────
    sentry_dsn: str = ""
    prometheus_enabled: bool = True

    # ── event id namespace (must match the EA's compiled-in value) ──────────
    event_id_namespace: str = "tradebridge.local"

    @field_validator("jwt_secret", "master_encryption_key")
    @classmethod
    def _reject_dev_secrets_in_prod(cls, v: str, info: object) -> str:
        return v

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def assert_production_safe(self) -> None:
        """Called at startup. Refuses to boot production with dev secrets."""
        if not self.is_production:
            return
        problems = []
        if "dev-only" in self.jwt_secret:
            problems.append("JWT_SECRET is still the development default")
        if "dev-only" in self.master_encryption_key:
            problems.append("MASTER_ENCRYPTION_KEY is still the development default")
        if self.enable_docs:
            problems.append("ENABLE_DOCS must be false in production")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

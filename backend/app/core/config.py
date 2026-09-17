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

    # ── MetaTrader link ─────────────────────────────────────────────────────
    ea_timestamp_skew_sec: int = 120
    ea_nonce_ttl_sec: int = 300
    ea_event_batch_max: int = 500
    #: A terminal quieter than this is shown as not syncing.
    heartbeat_timeout_sec: int = 600

    # ── cloud history provider (MetaApi) ────────────────────────────────────
    #: A trader on an iPad cannot run an Expert Advisor -- MetaTrader's mobile apps
    #: have no EA host. For them the terminal lives in the provider's cloud and we
    #: read history over HTTPS. Empty token means the feature is simply switched off.
    metaapi_token: str = ""
    metaapi_provisioning_url: str = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
    metaapi_client_url: str = "https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai"
    #: How often a connected account is polled for new deals.
    provider_poll_interval_sec: int = 300
    #: Re-read this far back every poll. Brokers book swap and commission late, so a
    #: closed trade's true cost keeps moving for days after the fill.
    provider_overlap_hours: int = 48
    #: How long one sync may spend reading before it saves its place and returns.
    #: Must stay under the platform's function timeout -- Vercel's own FastAPI
    #: example uses 60 seconds, so this leaves room to finish the database work.
    provider_sync_budget_sec: float = 40.0
    #: How far back the first import reaches. Ten years covers any real account.
    provider_backfill_years: int = 10

    #: True on a platform that gives each request its own short-lived process
    #: (Vercel and friends). Turns off connection pooling, which such a platform
    #: punishes rather than rewards.
    serverless: bool = False

    #: Shared secret for the HTTP scheduler endpoints. Empty means they 404, which
    #: is the right default: an open scheduler endpoint drives the database for free.
    cron_secret: str = ""

    # ── observability ───────────────────────────────────────────────────────
    sentry_dsn: str = ""
    prometheus_enabled: bool = True

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

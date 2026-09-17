"""Application configuration. Every value comes from the environment.

Where the platform already knows something, it is read rather than asked for. A
hosting platform that provisions a database writes the connection string into the
environment under its own name; making an operator copy that into a second variable
is a step that can only be got wrong, usually by picking the unpooled URL.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Connection strings the platform injects, best first. Vercel's Postgres and
#: Supabase integrations both write POSTGRES_URL, and both make it the *pooled*
#: endpoint -- which is the one a serverless function must use.
_POSTGRES_ENV_NAMES = ("POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING")
#: Upstash writes one of these depending on how it was added.
_REDIS_ENV_NAMES = ("KV_URL", "REDIS_URL", "UPSTASH_REDIS_URL")

#: asyncpg does not understand libpq's URL parameters and raises TypeError on them.
#: sslmode is translated; the rest are libpq-only and are dropped.
_LIBPQ_ONLY_PARAMS = ("channel_binding", "connect_timeout", "target_session_attrs")


def normalise_postgres_url(url: str) -> str:
    """Turn a platform-issued Postgres URL into one asyncpg can actually open.

    Three things are wrong with the string every provider hands out:

      scheme      `postgres://` and `postgresql://` select SQLAlchemy's *sync*
                  driver, which cannot be used from async code at all.
      sslmode     libpq's spelling. asyncpg takes `ssl`, and raises TypeError --
                  not a connection error, a crash -- when handed `sslmode`.
      extras      `channel_binding` and friends are libpq-only and do the same.

    Every one of these produces a failure at the first query rather than at
    startup, which is the worst moment to discover a typo in a URL.
    """
    if not url:
        return url

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    params = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            # asyncpg has no "prefer"; anything that is not explicitly disabled
            # should stay encrypted, which is the safe direction to round.
            params.append(("ssl", "disable" if value in ("disable", "allow") else "require"))
        elif key in _LIBPQ_ONLY_PARAMS:
            continue
        else:
            params.append((key, value))

    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


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

    @model_validator(mode="after")
    def _take_what_the_platform_already_knows(self) -> Settings:
        """Fill in from the hosting platform anything not set explicitly.

        Only ever fills gaps: an explicitly configured value always wins, so this
        cannot surprise a deployment that knows its own mind.
        """
        explicit = self.model_fields_set

        if "database_url" not in explicit:
            for name in _POSTGRES_ENV_NAMES:
                if os.environ.get(name):
                    self.database_url = os.environ[name]
                    break
        self.database_url = normalise_postgres_url(self.database_url)

        if "redis_url" not in explicit:
            for name in _REDIS_ENV_NAMES:
                if os.environ.get(name):
                    self.redis_url = os.environ[name]
                    break

        # VERCEL is set on every Vercel build and runtime. Nothing there is
        # long-lived, so its own pool is wrong and the platform's pooler is right.
        if "serverless" not in explicit and os.environ.get("VERCEL"):
            self.serverless = True

        # VERCEL_ENV is production / preview / development. A preview deployment is
        # not production: it should keep its docs and its friendlier errors.
        if "env" not in explicit and os.environ.get("VERCEL_ENV") == "production":
            self.env = "production"

        # Interactive API docs are a development convenience, not something to
        # publish by default on a live site.
        if "enable_docs" not in explicit and self.env == "production":
            self.enable_docs = False

        return self

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

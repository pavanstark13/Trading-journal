"""Reading configuration the hosting platform already set.

Every variable an operator has to copy by hand is one that can be got wrong, and the
connection strings are the worst of them: pick the unpooled URL and it works in
testing then collapses under load. So they are read, not asked for.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings, normalise_postgres_url


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    for name in (
        "DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING",
        "REDIS_URL", "KV_URL", "UPSTASH_REDIS_URL", "VERCEL", "VERCEL_ENV",
        "SERVERLESS", "ENV", "ENABLE_DOCS",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── the URL every provider hands out, and why it does not work as given ─────────
def test_a_vercel_postgres_url_is_made_usable() -> None:
    """What Vercel and Supabase actually write into POSTGRES_URL."""
    given = "postgres://user:pw@ep-cool-1-pooler.eu-central-1.aws.neon.tech/journal?sslmode=require"
    got = normalise_postgres_url(given)
    # postgres:// selects SQLAlchemy's *sync* driver, unusable from async code.
    assert got.startswith("postgresql+asyncpg://")
    # asyncpg raises TypeError on sslmode -- a crash, not a connection error.
    assert "sslmode" not in got
    assert "ssl=require" in got
    assert "ep-cool-1-pooler" in got          # the pooled host is preserved


def test_neons_extra_parameters_are_dropped() -> None:
    """channel_binding is libpq-only and asyncpg rejects it the same way."""
    got = normalise_postgres_url(
        "postgresql://u:p@host/db?sslmode=require&channel_binding=require"
    )
    assert "channel_binding" not in got
    assert "ssl=require" in got


def test_ssl_is_only_disabled_when_it_was_explicitly_disabled() -> None:
    assert "ssl=disable" in normalise_postgres_url("postgres://u:p@h/d?sslmode=disable")
    # "prefer" has no asyncpg equivalent; staying encrypted is the safe rounding.
    assert "ssl=require" in normalise_postgres_url("postgres://u:p@h/d?sslmode=prefer")


def test_a_url_that_is_already_right_is_left_alone() -> None:
    url = "postgresql+asyncpg://app@127.0.0.1:5432/journal"
    assert normalise_postgres_url(url) == url
    assert normalise_postgres_url("") == ""


# ── filling gaps from the platform ──────────────────────────────────────────────
def test_the_pooled_url_is_preferred_over_the_direct_one(monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_URL", "postgres://u:p@pooled-host/db?sslmode=require")
    monkeypatch.setenv("POSTGRES_URL_NON_POOLING", "postgres://u:p@direct-host/db")
    assert "pooled-host" in _settings().database_url


def test_an_explicit_setting_always_wins(monkeypatch) -> None:
    """Filling gaps must never override a deployment that knows its own mind."""
    monkeypatch.setenv("POSTGRES_URL", "postgres://u:p@platform-host/db")
    chosen = "postgresql+asyncpg://app@my-own-host/db"
    assert _settings(database_url=chosen).database_url == chosen


def test_redis_is_taken_from_whichever_name_upstash_used(monkeypatch) -> None:
    monkeypatch.setenv("KV_URL", "rediss://default:token@fond-cat.upstash.io:6379")
    assert _settings().redis_url.startswith("rediss://")


def test_running_on_vercel_turns_off_our_own_pooling(monkeypatch) -> None:
    assert _settings().serverless is False
    monkeypatch.setenv("VERCEL", "1")
    assert _settings().serverless is True


def test_only_a_production_deployment_is_treated_as_production(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    preview = _settings()
    assert preview.env == "development"      # a preview is not live
    assert preview.enable_docs is True

    monkeypatch.setenv("VERCEL_ENV", "production")
    live = _settings()
    assert live.env == "production"
    assert live.enable_docs is False         # docs are not published by default

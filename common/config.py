"""
Centralized configuration loader.

Every module imports `config` from here rather than reading os.environ
directly. This gives us:
  - one place to document every env var the system understands
  - type coercion (ints, booleans) at load time, not at use site
  - fail-fast validation — missing required vars raise at import time

Usage
-----
    from common.config import settings
    conn = psycopg2.connect(settings.DATABASE_URL)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Load .env from repo root. find_dotenv() would also work but anchoring to
# this file's location is more predictable when scripts are invoked from
# various working directories (cron, one-off runs, tests).
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"See .env.example."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Env var {name!r} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # Database
    DATABASE_URL: str
    DB_POOL_MIN: int
    DB_POOL_MAX: int

    # DataForSEO
    DATAFORSEO_LOGIN: str
    DATAFORSEO_PASSWORD: str
    DATAFORSEO_DEFAULT_QUEUE: str
    DATAFORSEO_LOCATION_CODE: int
    DATAFORSEO_LANGUAGE_CODE: str
    DATAFORSEO_DEVICE: str

    # GSC
    GSC_CLIENT_SECRETS_FILE: str
    GSC_TOKEN_FILE: str
    GSC_SITE_URL: str

    # PageSpeed
    PAGESPEED_API_KEY: str

    # Anthropic
    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL_CHEAP: str
    CLAUDE_MODEL_DEFAULT: str
    CLAUDE_MODEL_COMPLEX: str

    # Notifications (all optional)
    SLACK_WEBHOOK_URL: str
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USER: str
    SMTP_PASSWORD: str
    SMTP_FROM: str

    # Runtime
    LOG_LEVEL: str
    OUTPUTS_DIR: Path
    REPO_ROOT: Path


def _load() -> Settings:
    outputs_dir = Path(_optional("OUTPUTS_DIR", str(_REPO_ROOT / "outputs")))
    if not outputs_dir.is_absolute():
        outputs_dir = (_REPO_ROOT / outputs_dir).resolve()

    return Settings(
        DATABASE_URL=_require("DATABASE_URL"),
        DB_POOL_MIN=_int("DB_POOL_MIN", 1),
        DB_POOL_MAX=_int("DB_POOL_MAX", 5),

        DATAFORSEO_LOGIN=_require("DATAFORSEO_LOGIN"),
        DATAFORSEO_PASSWORD=_require("DATAFORSEO_PASSWORD"),
        DATAFORSEO_DEFAULT_QUEUE=_optional("DATAFORSEO_DEFAULT_QUEUE", "standard").lower(),
        DATAFORSEO_LOCATION_CODE=_int("DATAFORSEO_LOCATION_CODE", 2840),
        DATAFORSEO_LANGUAGE_CODE=_optional("DATAFORSEO_LANGUAGE_CODE", "en"),
        DATAFORSEO_DEVICE=_optional("DATAFORSEO_DEVICE", "desktop"),

        GSC_CLIENT_SECRETS_FILE=_optional("GSC_CLIENT_SECRETS_FILE", "./secrets/gsc_client_secrets.json"),
        GSC_TOKEN_FILE=_optional("GSC_TOKEN_FILE", "./secrets/gsc_token.json"),
        GSC_SITE_URL=_optional("GSC_SITE_URL"),

        PAGESPEED_API_KEY=_optional("PAGESPEED_API_KEY"),

        ANTHROPIC_API_KEY=_optional("ANTHROPIC_API_KEY"),
        CLAUDE_MODEL_CHEAP=_optional("CLAUDE_MODEL_CHEAP", "claude-haiku-4-5-20251001"),
        CLAUDE_MODEL_DEFAULT=_optional("CLAUDE_MODEL_DEFAULT", "claude-sonnet-4-6"),
        CLAUDE_MODEL_COMPLEX=_optional("CLAUDE_MODEL_COMPLEX", "claude-opus-4-6"),

        SLACK_WEBHOOK_URL=_optional("SLACK_WEBHOOK_URL"),
        SMTP_HOST=_optional("SMTP_HOST"),
        SMTP_PORT=_int("SMTP_PORT", 587),
        SMTP_USER=_optional("SMTP_USER"),
        SMTP_PASSWORD=_optional("SMTP_PASSWORD"),
        SMTP_FROM=_optional("SMTP_FROM"),

        LOG_LEVEL=_optional("LOG_LEVEL", "INFO").upper(),
        OUTPUTS_DIR=outputs_dir,
        REPO_ROOT=_REPO_ROOT,
    )


settings = _load()

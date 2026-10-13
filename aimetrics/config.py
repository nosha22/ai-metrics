"""Configuration: secrets from environment variables, organization (teams, people, repos) from YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://aimetrics:aimetrics@localhost:5432/aimetrics"))
    github_token: str | None = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_api: str = field(default_factory=lambda: _env("GITHUB_API", "https://api.github.com"))
    anthropic_admin_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_ADMIN_KEY"))
    cursor_admin_key: str | None = field(default_factory=lambda: _env("CURSOR_ADMIN_KEY"))
    otel_token: str | None = field(default_factory=lambda: _env("OTEL_INGEST_TOKEN"))
    events_token: str | None = field(default_factory=lambda: _env("EVENTS_TOKEN"))
    org_file: Path = field(default_factory=lambda: Path(_env("ORG_FILE", "config/organization.yml")))
    repos_dir: Path = field(default_factory=lambda: Path(_env("REPOS_DIR", "/data/repos")))
    lookback_days: int = field(default_factory=lambda: int(_env("LOOKBACK_DAYS", "30")))
    git_ai_bin: str = field(default_factory=lambda: _env("GIT_AI_BIN", "git-ai"))


def load_org(path: Path) -> dict:
    """Reads config/organization.yml. The structure is documented in config/organization.example.yml."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist — copy config/organization.example.yml")
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("teams", [])
    data.setdefault("people", [])
    data.setdefault("repositories", [])
    data.setdefault("detection", {})
    return data

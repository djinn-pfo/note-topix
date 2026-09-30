"""Deployment-specific settings, read from .env at the repo root.

Everything here is per-installation (which Raindrop collection to read, which
note.com account is yours), so it lives in .env rather than in the code. Real
environment variables win over .env, which keeps cron and one-off overrides
working (`NOTE_USER=other uv run scripts/articles.py import`).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


@lru_cache(maxsize=1)
def dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env(key: str, default: str = "") -> str:
    """Real environment first, then .env, then the default."""
    return os.environ.get(key) or dotenv().get(key) or default

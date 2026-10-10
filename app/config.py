"""Minimal configuration loading.

Values come from the process environment first, then from `shopmate/.env`
(git-ignored). The parser is deliberately tiny (KEY=VALUE lines, `#` comments)
so the project does not need python-dotenv.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_API_URL = "http://localhost:8000/graphql/"
VN_CHANNEL_SLUG = "vn"
# The retrieval mode used when a caller does not name one (Retriever.search). Phase 4 adopted dense_folded
# (docs/RESULTS-phase4-exp1.md). A constant, not an environment setting, so no deployment can silently change
# what "default retrieval" means. The evaluation runners pass their mode explicitly and do not depend on it.
DEFAULT_RETRIEVAL_MODE = "dense_folded"


class ConfigError(Exception):
    """A required setting is missing."""


def read_env_file(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Parse KEY=VALUE lines. Missing file means no values."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def get_setting(name: str, default: str | None = None, env_file: Path = DEFAULT_ENV_FILE) -> str | None:
    """Return a setting from the environment, then .env, then `default`. Empty counts as unset."""
    value = os.environ.get(name) or read_env_file(env_file).get(name)
    return value or default


def require_setting(name: str, env_file: Path = DEFAULT_ENV_FILE) -> str:
    value = get_setting(name, env_file=env_file)
    if not value:
        raise ConfigError(f"{name} is not set (environment or {env_file})")
    return value


def api_url() -> str:
    return get_setting("SALEOR_API_URL", DEFAULT_API_URL)

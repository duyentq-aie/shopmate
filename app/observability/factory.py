"""Turning Langfuse on (docs/DECISIONS-langfuse.md, L1).

`build_sink()` returns a LangfuseSink when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are both set (environment first, then .env, like every other
setting), and None otherwise. With None the system runs exactly as before and `langfuse` is not even imported, so CI (which has no key) sends nothing.
The host is LANGFUSE_HOST, then LANGFUSE_BASE_URL (the name the SDK prefers), then the SDK's own default (Langfuse Cloud).

A client that cannot be built, or an SDK that is missing, is logged as a warning (the class of the failure; its message could hold a key) and
gives None: observability never stops the application from starting. Nothing here is called by a test or a library on its own: the entry point
of the chat API passes the result to its Tracer."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import DEFAULT_ENV_FILE, get_setting
from app.observability.masking import sdk_mask
from app.observability.sink import LangfuseSink
from app.tracing import MIN_SECRET_LENGTH, SECRET_NAME, known_secrets

LOG = logging.getLogger("shopmate.observability")
PLACEHOLDER = "your-"  # .env.example ships "your-langfuse-public-key": copied and not filled in means "off", not "wrong key"


def load_langfuse() -> Any:
    """The two names of the SDK the sink is built on, imported only now."""
    import langfuse

    return SimpleNamespace(Langfuse=langfuse.Langfuse, propagate_attributes=langfuse.propagate_attributes)


def _settings(env: Mapping[str, str] | None, env_file: Path) -> tuple[Callable[[str], str | None], list[str]]:
    if env is None:
        return (lambda name: get_setting(name, env_file=env_file)), known_secrets(env_file)
    secrets = [v for k, v in env.items() if SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LENGTH]
    return (lambda name: env.get(name) or None), secrets


def _configured(value: str | None) -> str | None:
    return None if not value or value.startswith(PLACEHOLDER) else value


class Connection:
    """A configured client with the SDK it came from and every secret to mask."""

    def __init__(self, client: Any, sdk: Any, secrets: Sequence[str]) -> None:
        self.client, self.sdk, self.secrets = client, sdk, tuple(secrets)


def connect(env: Mapping[str, str] | None = None, *, env_file: Path = DEFAULT_ENV_FILE, secrets: Sequence[str] = (),
            load_sdk: Callable[[], Any] = load_langfuse) -> Connection | None:
    """A client with the SDK-level mask installed, or None (no keys, SDK missing, client not startable: logged, never raised).
    `env` replaces the environment and .env (tests); None reads os.environ and `env_file`."""
    setting, known = _settings(env, env_file)
    public_key, secret_key = _configured(setting("LANGFUSE_PUBLIC_KEY")), _configured(setting("LANGFUSE_SECRET_KEY"))
    if not (public_key and secret_key):
        return None
    all_secrets = sorted({*known, *secrets, secret_key}, key=len, reverse=True)
    try:
        sdk = load_sdk()
        options: dict[str, Any] = {"public_key": public_key, "secret_key": secret_key, "mask": sdk_mask(all_secrets)}
        base_url = _configured(setting("LANGFUSE_HOST")) or _configured(setting("LANGFUSE_BASE_URL"))
        if base_url:
            options["base_url"] = base_url
        return Connection(sdk.Langfuse(**options), sdk, all_secrets)
    except Exception as exc:  # noqa: BLE001 - never stop the application over observability
        LOG.warning("langfuse is off: it could not be started (%s)", type(exc).__name__)
        return None


def build_sink(env: Mapping[str, str] | None = None, *, env_file: Path = DEFAULT_ENV_FILE, secrets: Sequence[str] = (),
               load_sdk: Callable[[], Any] = load_langfuse) -> LangfuseSink | None:
    connection = connect(env, env_file=env_file, secrets=secrets, load_sdk=load_sdk)
    if connection is None:
        return None
    return LangfuseSink(connection.client, propagate_attributes=connection.sdk.propagate_attributes, secrets=connection.secrets)

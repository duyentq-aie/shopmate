"""Turning Langfuse on (docs/DECISIONS-langfuse.md, L1): no keys means no sink and no `import langfuse` at all, so the system runs as before and CI
sends nothing. Settings come from the environment, then .env (the same order as every other setting); tests pass them in, they never read the real .env."""

from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.observability.factory import build_sink, connect
from app.observability.sink import LangfuseSink

REPO = Path(__file__).resolve().parent.parent
KEYS = {"LANGFUSE_PUBLIC_KEY": "test-public-key-" + "0" * 12, "LANGFUSE_SECRET_KEY": "test-secret-key-" + "1" * 12}


class FakeLangfuse:
    instances: list["FakeLangfuse"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeLangfuse.instances.append(self)


def fake_sdk():
    FakeLangfuse.instances = []
    return SimpleNamespace(Langfuse=FakeLangfuse, propagate_attributes=lambda **kw: nullcontext())


def never_loaded():
    raise AssertionError("the SDK must not be loaded when Langfuse is off")


@pytest.mark.parametrize("env", [
    {},
    {"LANGFUSE_PUBLIC_KEY": KEYS["LANGFUSE_PUBLIC_KEY"]},
    {"LANGFUSE_SECRET_KEY": KEYS["LANGFUSE_SECRET_KEY"]},
    {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""},
    {"LANGFUSE_PUBLIC_KEY": "your-langfuse-public-key", "LANGFUSE_SECRET_KEY": "your-langfuse-secret-key"},  # .env.example copied and not filled in
])
def test_without_both_keys_there_is_no_sink_and_the_sdk_is_never_loaded(env):
    assert build_sink(env, load_sdk=never_loaded) is None


def test_with_both_keys_the_sink_wraps_a_client_built_from_them_and_a_mask():
    sink = build_sink({**KEYS, "LANGFUSE_HOST": "https://langfuse.example.test"}, load_sdk=fake_sdk)
    assert isinstance(sink, LangfuseSink)
    (client,) = FakeLangfuse.instances
    assert sink.client is client
    assert client.kwargs["public_key"] == KEYS["LANGFUSE_PUBLIC_KEY"] and client.kwargs["secret_key"] == KEYS["LANGFUSE_SECRET_KEY"]
    assert client.kwargs["base_url"] == "https://langfuse.example.test"
    assert client.kwargs["mask"](data={"x": "khachhang02@example.com"}) == {"x": "[email]"}  # the SDK-level layer is installed


def test_the_host_comes_from_langfuse_host_then_langfuse_base_url_then_the_sdk_default():
    build_sink({**KEYS, "LANGFUSE_HOST": "https://a.test", "LANGFUSE_BASE_URL": "https://b.test"}, load_sdk=fake_sdk)
    assert FakeLangfuse.instances[0].kwargs["base_url"] == "https://a.test"
    build_sink({**KEYS, "LANGFUSE_BASE_URL": "https://b.test"}, load_sdk=fake_sdk)
    assert FakeLangfuse.instances[0].kwargs["base_url"] == "https://b.test"
    build_sink(KEYS, load_sdk=fake_sdk)
    assert "base_url" not in FakeLangfuse.instances[0].kwargs


def test_the_secrets_in_the_settings_are_masked_in_everything_sent():
    sink = build_sink({**KEYS, "OTHER_API_TOKEN": "tok-" + "9" * 12}, load_sdk=fake_sdk, secrets=["extra-secret-value"])
    assert set(sink.secrets) >= {KEYS["LANGFUSE_SECRET_KEY"], "tok-" + "9" * 12, "extra-secret-value"}


def test_a_client_that_cannot_be_built_leaves_the_system_running_without_langfuse(caplog):
    class Broken:
        def __init__(self, **kwargs):
            raise RuntimeError(f"cannot start with {kwargs['secret_key']}")

    sdk = SimpleNamespace(Langfuse=Broken, propagate_attributes=lambda **kw: nullcontext())
    with caplog.at_level(logging.WARNING, logger="shopmate.observability"):
        assert build_sink(KEYS, load_sdk=lambda: sdk) is None
    assert "RuntimeError" in caplog.text and KEYS["LANGFUSE_SECRET_KEY"] not in caplog.text


def test_a_missing_sdk_leaves_the_system_running_without_langfuse(caplog):
    def missing():
        raise ImportError("No module named 'langfuse'")

    with caplog.at_level(logging.WARNING, logger="shopmate.observability"):
        assert build_sink(KEYS, load_sdk=missing) is None
    assert "ImportError" in caplog.text


def test_the_settings_are_read_from_the_environment_then_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"LANGFUSE_PUBLIC_KEY={KEYS['LANGFUSE_PUBLIC_KEY']}\nLANGFUSE_SECRET_KEY=from-the-file-0000\nLANGFUSE_HOST=https://file.test\n", encoding="utf-8")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "from-the-environment-0000")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    build_sink(env_file=env_file, load_sdk=fake_sdk)
    kwargs = FakeLangfuse.instances[0].kwargs
    assert kwargs["public_key"] == KEYS["LANGFUSE_PUBLIC_KEY"] and kwargs["secret_key"] == "from-the-environment-0000" and kwargs["base_url"] == "https://file.test"


def test_with_no_keys_langfuse_is_not_even_imported():
    code = ("import sys\nfrom app.observability.factory import build_sink\n"
            "assert build_sink({}) is None\n"
            "assert not any(m == 'langfuse' or m.startswith('langfuse.') for m in sys.modules), 'langfuse was imported'\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_the_real_sdk_loads_and_has_what_the_sink_needs():
    """The default loader imports `langfuse` (4.17.0, pinned in pyproject.toml) and takes the two names the sink is built on."""
    from app.observability.factory import load_langfuse

    sdk = load_langfuse()
    assert callable(sdk.Langfuse) and callable(sdk.propagate_attributes) and callable(sdk.Langfuse.create_trace_id)


# --- the connection the evaluation script shares with the sink -------------------------------------------------------------------------------


def test_connect_gives_the_masked_client_the_sdk_and_the_secrets_and_none_without_keys():
    assert connect({}, load_sdk=never_loaded) is None
    connection = connect({**KEYS, "LANGFUSE_HOST": "https://langfuse.example.test"}, load_sdk=fake_sdk)
    assert connection.client is FakeLangfuse.instances[0] and callable(connection.sdk.propagate_attributes)
    assert connection.client.kwargs["mask"](data="khachhang02@example.com") == "[email]"
    assert KEYS["LANGFUSE_SECRET_KEY"] in connection.secrets


def test_the_sink_is_built_on_the_same_connection():
    sink = build_sink(KEYS, load_sdk=fake_sdk)
    assert sink.client is FakeLangfuse.instances[0] and sink.secrets == tuple(connect(KEYS, load_sdk=fake_sdk).secrets)

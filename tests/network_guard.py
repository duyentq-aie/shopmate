"""CI guard: with SHOPMATE_BLOCK_NETWORK=1 a test that tries to reach a public address fails at once instead of waiting for a timeout (or, worse, succeeding).

The only places that could do it are the Gemini client (built only for --live runs, never by a test without a fake transport) and the model download
(HF_HUB_OFFLINE). This makes that a fact the CI run checks every time, not just a habit of the code: a call to the API outside the cache must fail as
`llm_cache_miss_offline` (no client, so no network), never reach the internet. Loopback and private addresses stay allowed (the monitor tests start real servers)."""

from __future__ import annotations

import ipaddress
import socket


def is_public_address(address) -> bool:
    """True for a (host, port, ...) whose host is a global IP, or is not an IP literal at all (a name that would need a DNS lookup)."""
    if not isinstance(address, tuple) or not address:
        return False  # a unix socket path or something else that is not an internet address
    try:
        return ipaddress.ip_address(str(address[0]).split("%")[0]).is_global
    except ValueError:
        return True


def install(monkeypatch) -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refuse(address):
        raise OSError(f"SHOPMATE_BLOCK_NETWORK: a test tried to connect to {address!r}; unit tests must not reach the internet")

    def connect(self, address):
        if is_public_address(address):
            refuse(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if is_public_address(address):
            refuse(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

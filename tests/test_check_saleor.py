"""check_saleor.py: clean failure when the API resets connections, --wait, credentials."""

from __future__ import annotations

import json
import socket
import struct
import threading

import pytest

from scripts import check_saleor as cs

OK_BODY = json.dumps({"data": {"shop": {"name": "Saleor e-commerce"}}}).encode()


class FlakyServer:
    """Real TCP server. Per connection, the next behavior: 'reset' reads the whole
    request and then aborts with RST (so the client fails inside getresponse(),
    like a Saleor API that is still starting); 'ok' answers a GraphQL shop name."""

    def __init__(self, behaviors: list[str]):
        self.behaviors = list(behaviors)
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/graphql/"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _read_request(conn: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head, _, body = data.partition(b"\r\n\r\n")
        length = next(int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length"))
        while len(body) < length:
            body += conn.recv(4096)

    def _serve(self) -> None:
        while self.behaviors:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            behavior = self.behaviors.pop(0)
            self._read_request(conn)
            if behavior == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            else:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(OK_BODY)}\r\nConnection: close\r\n\r\n".encode() + OK_BODY)
            conn.close()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def server_factory():
    servers = []

    def make(behaviors):
        server = FlakyServer(behaviors)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_reset_during_response_is_a_clean_failure(server_factory):
    server = server_factory(["reset"])
    with pytest.raises(cs.ApiUnreachable, match="not reachable or still starting") as info:
        cs.graphql(server.url, "query ShopName { shop { name } }")
    assert isinstance(info.value.__cause__, (ConnectionError, OSError))


def test_main_exits_1_with_message_on_reset(server_factory, monkeypatch, capsys):
    server = server_factory(["reset"])
    monkeypatch.setenv("SALEOR_API_URL", server.url)
    assert cs.main([]) == 1
    assert "Saleor API not reachable or still starting" in capsys.readouterr().err


def test_connection_refused_is_a_clean_failure():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens on this port now
    with pytest.raises(cs.ApiUnreachable, match="not reachable"):
        cs.graphql(f"http://127.0.0.1:{port}/graphql/", "query ShopName { shop { name } }")


def test_wait_retries_until_the_api_answers(server_factory):
    server = server_factory(["reset", "reset", "ok"])
    sleeps: list[float] = []
    cs.wait_for_api(server.url, 30, sleep=sleeps.append)
    assert server.connections == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff


def test_wait_gives_up_after_deadline(server_factory):
    server = server_factory(["reset"] * 10)
    now = [0.0]

    def fake_sleep(seconds):
        now[0] += seconds

    with pytest.raises(cs.ApiUnreachable, match="gave up"):
        cs.wait_for_api(server.url, 2, sleep=fake_sleep, clock=lambda: now[0])
    assert server.connections < 10


def test_runtime_token_requests_must_be_single_queries():
    with pytest.raises(cs.CheckError, match="refused"):
        cs.runtime_query("http://unused.invalid/", "Cancel", 'mutation Cancel { orderCancel(id: "x") { order { id } } }', "t")


def test_runtime_token_from_settings_is_preferred(monkeypatch):
    monkeypatch.setattr(cs, "get_setting", lambda name, default=None: "rt-token" if name == "SALEOR_RUNTIME_TOKEN" else default)
    monkeypatch.setenv("SALEOR_STAFF_EMAIL", "admin@example.com")
    monkeypatch.setenv("SALEOR_STAFF_PASSWORD", "x")
    assert cs.get_token("http://unused.invalid/") == ("rt-token", True)


def test_missing_credentials_is_a_config_error(monkeypatch):
    monkeypatch.setattr(cs, "get_setting", lambda name, default=None: default)
    monkeypatch.delenv("SALEOR_STAFF_EMAIL", raising=False)
    monkeypatch.delenv("SALEOR_STAFF_PASSWORD", raising=False)
    with pytest.raises(cs.ConfigError, match="SALEOR_RUNTIME_TOKEN"):
        cs.get_token("http://unused.invalid/")

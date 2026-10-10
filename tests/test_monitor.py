"""The local monitor (app/monitor.py): a read-only view of logs/traces.jsonl and dashboard/index.html, loopback only.

Most tests drive the ASGI app through Starlette's TestClient with a stated client address; the bind and the
"cannot be exposed" claims are also proved with REAL uvicorn servers on real sockets. Tests that look at the page use the
real dashboard/index.html of the repository, not a stand-in.
"""

from __future__ import annotations

import asyncio
import builtins
import random
import re
import socket
import threading
import time
import tracemalloc
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from app import monitor
from app.config import REPO_ROOT
from app.monitor import (CSP, DASHBOARD_PAGE, MAX_LIMIT, create_app, is_loopback_host, is_loopback_ip, make_config, parse_bind,
                         tail_lines)
from app.tracing import TRACE_PATH

LOCAL = ("127.0.0.1", 50000)
PAGE = b"<!doctype html><title>stand-in page</title>"
EXPECTED_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")


def _lines(n, start=0, pad=150):
    return [f'{{"i":{i},"pad":"{"x" * pad}"}}\n'.encode() for i in range(start, start + n)]


def _write(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))
    return path


@pytest.fixture
def site(tmp_path):
    """A trace file and a stand-in page in a temp dir, next to a file that must never be served."""
    (tmp_path / "secret.txt").write_text("TOP-SECRET-SENTINEL", encoding="utf-8")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=TOP-SECRET-SENTINEL", encoding="utf-8")
    page = tmp_path / "dashboard" / "index.html"
    page.parent.mkdir()
    page.write_bytes(PAGE)
    traces = _write(tmp_path / "logs" / "traces.jsonl", _lines(6000))
    return create_app(traces_path=traces, page_path=page), traces, page, tmp_path


def _client(app, client=LOCAL, **kwargs):
    return TestClient(app, client=client, follow_redirects=False, **kwargs)


# --- tail_lines -----------------------------------------------------------------------------------------------------------------


def test_the_tail_of_a_large_file_is_exact_and_costs_far_less_memory_than_the_file(tmp_path):
    lines = _lines(200_000)  # about 34 MB
    path = _write(tmp_path / "big.jsonl", lines)
    size = path.stat().st_size
    assert size > 30_000_000
    for limit, budget in ((5000, 4_000_000), (20_000, 10_000_000)):
        tracemalloc.start()
        try:
            got = tail_lines(path, limit)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert got == b"".join(lines[-limit:])
        assert peak < budget < size / 3, (limit, peak)  # bounded by the lines asked for, not by the size of the file


def test_the_default_and_the_maximum_limit_are_the_agreed_numbers():
    assert MAX_LIMIT == 20_000 and monitor.DEFAULT_LIMIT == 5000


@pytest.mark.parametrize("seed", range(40))
def test_tail_matches_a_naive_reference_for_random_files_limits_and_block_sizes(tmp_path, seed):
    rng = random.Random(seed)
    lines = [(("é" * rng.randint(0, 40)) + "ệ̣" * rng.randint(0, 5)).encode() + b"\n" for _ in range(rng.randint(0, 60))]
    tail = rng.choice([b"", b"", b"unterminated"])
    path = _write(tmp_path / "t.jsonl", lines + ([tail] if tail else []))
    limit, block = rng.randint(1, 80), rng.choice([1, 2, 3, 7, 64, 4096])
    assert tail_lines(path, limit, block=block) == b"".join(lines[-limit:])  # the unterminated last line is never part of it


def test_a_file_with_fewer_lines_than_the_limit_is_returned_whole_and_limit_one_is_the_last_line(tmp_path):
    lines = _lines(7)
    path = _write(tmp_path / "t.jsonl", lines)
    assert tail_lines(path, 5000) == b"".join(lines) and tail_lines(path, 7) == b"".join(lines)
    assert tail_lines(path, 1) == lines[-1] and tail_lines(path, 6) == b"".join(lines[1:])


def test_a_missing_empty_or_newline_free_file_gives_nothing(tmp_path):
    assert tail_lines(tmp_path / "nope.jsonl", 10) == b""
    assert tail_lines(_write(tmp_path / "empty.jsonl", []), 10) == b""
    assert tail_lines(_write(tmp_path / "partial.jsonl", [b'{"half": "a trace being writ']), 10) == b""
    assert tail_lines(_write(tmp_path / "nl.jsonl", [b"\n"]), 10) == b"\n"


def test_a_line_being_written_is_left_out_and_the_ones_before_it_are_served(tmp_path):
    lines = _lines(5)
    path = _write(tmp_path / "t.jsonl", lines + [b'{"i":5,"pad":"half a li'])
    assert tail_lines(path, 3) == b"".join(lines[-3:])


def test_the_size_cap_keeps_the_newest_whole_lines_and_gives_up_on_a_line_bigger_than_the_cap(tmp_path):
    lines = _lines(100, pad=100)  # about 120 bytes each
    path = _write(tmp_path / "t.jsonl", lines)
    got = tail_lines(path, 100, max_bytes=1000)
    assert 0 < len(got) <= 1000 and got.endswith(lines[-1]) and got == b"".join(lines[-len(got.splitlines()):])
    assert got.startswith(b'{"i":') and got.count(b"\n") == len(got.splitlines())  # whole lines only
    huge = _write(tmp_path / "huge.jsonl", [b"x" * 5000 + b"\n"])
    assert tail_lines(huge, 10, max_bytes=1000) == b""


def test_a_line_cut_by_a_block_boundary_is_never_served_even_when_the_size_cap_stops_the_reading_early(tmp_path):
    lines = [b"L%08d\n" % i for i in range(40)]  # 10 bytes each, all different
    path = _write(tmp_path / "t.jsonl", lines + [b"y" * 30])  # and a long last line that is still being written
    got = tail_lines(path, 100, max_bytes=40, block=16)  # reading stops after 64 bytes, which begin in the middle of a line
    assert got and set(got.splitlines(keepends=True)) <= set(lines) and got == b"".join(lines)[-len(got):]


def test_the_tail_is_a_snapshot_a_line_appended_while_reading_does_not_corrupt_it(tmp_path, monkeypatch):
    lines = _lines(50)
    path = _write(tmp_path / "t.jsonl", lines)
    real_fstat, appended = monitor.os.fstat, []

    def fstat_then_a_writer_appends(fd):
        size = real_fstat(fd)  # the reader has taken the size ...
        if not appended:
            with open(path, "ab") as out:  # ... and a writer appends a whole line and half of another
                out.write(b'{"i":999}\n{"i":1000,"half')
            appended.append(1)
        return size

    monkeypatch.setattr(monitor.os, "fstat", fstat_then_a_writer_appends)
    assert tail_lines(path, 5) == b"".join(lines[-5:])
    monkeypatch.undo()
    assert tail_lines(path, 2) == lines[-1] + b'{"i":999}\n'  # the next read sees the new complete line, still not the half one


# --- the routes -----------------------------------------------------------------------------------------------------------------


def test_the_api_returns_the_last_5000_lines_by_default_as_text_plain(site):
    app, traces, _, _ = site
    response = _client(app).get("/monitor/api/traces")
    assert response.status_code == 200 and response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.content == b"".join(_lines(6000)[-5000:])


@pytest.mark.parametrize("limit, count", [(1, 1), (10, 10), (5000, 5000), (20_000, 6000)])
def test_limit_selects_how_many_lines_up_to_the_maximum(site, limit, count):
    app, *_ = site
    body = _client(app).get("/monitor/api/traces", params={"limit": limit}).content
    assert body.count(b"\n") == count and body.endswith(_lines(6000)[-1])


@pytest.mark.parametrize("limit", ["0", "-1", "20001", "1000000", "abc", "1.5", "", "0x10", "null", "5000abc"])
def test_a_limit_outside_1_to_20000_or_not_a_whole_number_is_422(site, limit):
    app, *_ = site
    response = _client(app).get("/monitor/api/traces", params={"limit": limit})
    assert response.status_code == 422, limit
    assert "TOP-SECRET" not in response.text and b"xxxx" not in response.content


def test_a_missing_or_empty_trace_file_is_200_with_an_empty_body(tmp_path):
    page = _write(tmp_path / "dashboard" / "index.html", [PAGE])
    missing = _client(create_app(traces_path=tmp_path / "logs" / "traces.jsonl", page_path=page)).get("/monitor/api/traces")
    assert (missing.status_code, missing.content) == (200, b"") and missing.headers["content-type"].startswith("text/plain")
    empty = _client(create_app(traces_path=_write(tmp_path / "e.jsonl", []), page_path=page)).get("/monitor/api/traces")
    assert (empty.status_code, empty.content) == (200, b"")


def test_the_page_is_served_at_monitor_slash_with_the_security_headers(site):
    app, _, _, _ = site
    response = _client(app).get("/monitor/")
    assert response.status_code == 200 and response.content == PAGE
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == EXPECTED_CSP == CSP
    assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"


def test_every_response_carries_no_store_nosniff_and_the_csp_even_errors_and_the_api(site):
    app, *_ = site
    client = _client(app)
    for path in ("/monitor/api/traces", "/monitor/api/traces?limit=0", "/nothing", "/monitor"):
        headers = client.get(path).headers
        assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff", path
        assert headers["content-security-policy"] == EXPECTED_CSP, path


def test_a_missing_page_is_404_and_says_nothing_about_the_disk(site):
    app, _, page, tmp_path = site
    page.unlink()
    response = _client(app).get("/monitor/")
    assert response.status_code == 404 and str(tmp_path) not in response.text


def test_monitor_without_the_slash_redirects_to_monitor_slash_and_the_relative_fetch_resolves_under_it(site):
    app, *_ = site
    client = _client(app)
    redirect = client.get("/monitor")
    assert redirect.status_code == 307
    landed = urljoin("http://127.0.0.1:8765/monitor", redirect.headers["location"])
    assert landed == "http://127.0.0.1:8765/monitor/"
    page_url = landed
    assert urljoin(page_url, "api/traces") == "http://127.0.0.1:8765/monitor/api/traces"  # what the page's relative fetch becomes
    assert urljoin("http://127.0.0.1:8765/monitor", "api/traces") == "http://127.0.0.1:8765/api/traces"  # ... and why the slash matters
    assert client.get("/api/traces").status_code == 404  # the wrong resolution finds nothing
    followed = TestClient(app, client=LOCAL).get("/monitor")  # a browser follows the redirect
    assert followed.status_code == 200 and followed.content == PAGE


def test_the_real_page_is_there_and_is_what_the_monitor_serves():
    assert DASHBOARD_PAGE == REPO_ROOT / "dashboard" / "index.html"
    assert DASHBOARD_PAGE.is_file(), "dashboard/index.html is missing: the monitor has nothing to serve"
    response = _client(create_app()).get("/monitor/")
    assert response.status_code == 200 and response.content == DASHBOARD_PAGE.read_bytes()
    assert response.headers["content-security-policy"] == EXPECTED_CSP


def test_the_real_pages_fetch_is_relative_and_resolves_to_the_traces_route_of_the_real_app(tmp_path):
    html = DASHBOARD_PAGE.read_text(encoding="utf-8")
    targets = re.findall(r"""fetch\(\s*[`'"]([^`'"]*api/traces[^`'"]*)""", html)
    assert targets, "the page does not fetch api/traces with a literal path"
    assert all(not t.startswith(("/", "http:", "https:", "//")) for t in targets), targets  # relative: the slash of /monitor/ matters
    client = _client(create_app(traces_path=_write(tmp_path / "logs" / "traces.jsonl", _lines(3))))
    for target in targets:
        resolved = urljoin("http://127.0.0.1:8765/monitor/", re.sub(r"\$\{[^}]*\}", "5", target))  # a template slot gets a number
        assert resolved.startswith("http://127.0.0.1:8765/monitor/api/traces"), resolved
        assert client.get(resolved.removeprefix("http://127.0.0.1:8765")).status_code == 200, resolved


def test_the_example_address_in_the_real_pages_help_text_is_the_monitors_default_address():
    html = DASHBOARD_PAGE.read_text(encoding="utf-8")
    ports = set(re.findall(r"http://[\w.]+:(\d+)/monitor/", html))
    assert ports == {str(monitor.DEFAULT_PORT)}, f"the page's help text names ports {sorted(ports)}, the monitor listens on {monitor.DEFAULT_PORT}"


def test_the_real_page_knows_every_error_type_the_tracer_can_write_and_no_other():
    from app.tracing import ERROR_TYPES

    html = DASHBOARD_PAGE.read_text(encoding="utf-8")
    block = re.search(r"const ERR = \{(.*?)\n\};", html, re.S).group(1)
    entries = {key: (label, sev) for key, label, sev in re.findall(r"^\s+([a-z0-9_]+):\s*\{label: '([^']*)', sev: '(\w+)'", block, re.M)}
    assert set(entries) == set(ERROR_TYPES), f"page only: {sorted(set(entries) - set(ERROR_TYPES))}, tracer only: {sorted(set(ERROR_TYPES) - set(entries))}"
    assert entries["request_interrupted"] == ("Request bị ngắt giữa chừng", "error")
    hint = re.search(r"error\.type đã biết: (.*?)\. Loại lạ", html).group(1)
    assert set(re.findall(r"[a-z0-9_]+", hint)) == set(ERROR_TYPES)  # the help text lists the same types


def _page_loads_only_what_the_csp_allows(html: str) -> list[str]:
    """What in the real page would break under CSP; [] when everything it loads is allowed."""
    problems = []
    for url in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html):
        if url.startswith(("#", "data:", "mailto:")) or url.startswith("monitor/") or not re.match(r"[a-z]+:|//", url):
            continue
        if not url.startswith("https://fonts.googleapis.com") and not url.startswith("https://fonts.gstatic.com"):
            problems.append(f"external reference {url}")
    problems += [f"@import {m}" for m in re.findall(r"@import\s+(?:url\()?['\"]?([^'\")\s;]+)", html) if not m.startswith("https://fonts.googleapis.com")]
    problems += [f"remote url() {m}" for m in re.findall(r"url\(\s*['\"]?(https?://[^'\")\s]+)", html)
                 if not m.startswith(("https://fonts.googleapis.com", "https://fonts.gstatic.com"))]
    problems += [f"remote connection {m}" for m in re.findall(r"""fetch\(\s*['"`](https?://[^'"`]+)""", html)]
    problems += [f"{what} would be blocked" for what, pattern in (("eval", r"\beval\s*\("), ("new Function", r"new\s+Function\s*\("),
                                                                  ("WebSocket", r"new\s+WebSocket"), ("EventSource", r"new\s+EventSource"),
                                                                  ("iframe", r"<iframe"), ("form action", r"<form[^>]+action=\s*['\"]https?://"),
                                                                  ("string timeout", r"setTimeout\(\s*['\"]")) if re.search(pattern, html)]
    return problems


def test_the_real_page_loads_nothing_the_agreed_csp_would_block():
    assert _page_loads_only_what_the_csp_allows(DASHBOARD_PAGE.read_text(encoding="utf-8")) == []


def test_the_csp_checker_itself_catches_what_it_is_meant_to(site):
    for bad in ('<script src="https://cdn.example.com/x.js"></script>', "<link rel=stylesheet href='https://evil.test/a.css'>",
                "<style>@import url('https://evil.test/a.css');</style>", "<script>fetch('https://evil.test/x')</script>",
                "<script>eval('1')</script>", "<iframe src=x></iframe>", "<script>new WebSocket('ws://x')</script>"):
        assert _page_loads_only_what_the_csp_allows(bad), bad
    ok = '<link href="https://fonts.googleapis.com/css2?family=X" rel=stylesheet><script>fetch("api/traces?limit=5")</script>'
    assert _page_loads_only_what_the_csp_allows(ok) == []


# --- nothing else can be read ---------------------------------------------------------------------------------------------------


def test_only_three_routes_exist_and_no_docs_or_listing(site):
    app, *_ = site
    api = app.app  # the FastAPI app behind the loopback guard
    assert {route.path for route in api.routes} == {"/monitor", "/monitor/", "/monitor/api/traces"}
    assert api.docs_url is None and api.redoc_url is None and api.openapi_url is None
    assert all("{" not in route.path for route in api.routes)  # no path parameters
    assert {m for route in api.routes for m in route.methods} == {"GET"}


ATTEMPTS = ["/", "/monitor/index.html", "/monitor/dashboard/index.html", "/monitor/api", "/monitor/api/", "/monitor/api/traces/",
            "/monitor/static/app.js", "/monitor/secret.txt", "/secret.txt", "/.env", "/monitor/.env", "/monitor/../.env",
            "/monitor/api/traces/../../../.env", "/monitor/%2e%2e/.env", "/monitor/%2e%2e%2f.env", "/monitor/..%2f..%2f.env",
            "/monitor/api/traces/%2e%2e/%2e%2e/secret.txt", "/dashboard/index.html", "/dashboard/", "/dashboard",
            "/logs/traces.jsonl", "/logs/", "/pyproject.toml", "/etc/passwd", "//etc/passwd", "/monitor//etc/passwd",
            "/monitor/..", "/monitor/.", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/favicon.ico",
            "/monitor/%00", "/monitor/api/traces%00.txt", "/monitor/api/traces;.env", "/monitor/api/traces?file=../.env",
            "/monitor/api/traces?path=/etc/passwd", "/monitor/api/traces?limit=5&path=.env"]


def _raw_get(app, raw_path: str, client=LOCAL, headers=()):
    """Send a request to the ASGI app with the path EXACTLY as written (no client-side normalisation of `..`)."""
    path, _, query = raw_path.partition("?")
    from urllib.parse import unquote
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": unquote(path), "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers], "client": client, "server": ("127.0.0.1", 8765)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict((k.decode(), v.decode()) for k, v in start["headers"]), body


def test_no_path_outside_the_page_and_the_trace_file_can_be_read(site):
    app, traces, page, tmp_path = site
    served = {"/monitor/api/traces": b"".join(_lines(6000)[-5000:]), "/monitor/": PAGE}
    for attempt in ATTEMPTS:
        status, _, body = _raw_get(app, attempt)
        assert b"TOP-SECRET" not in body and b"root:" not in body and str(tmp_path).encode() not in body, attempt
        path, _, query = attempt.partition("?")
        if path in served:
            # an unknown query key is ignored: the file is still the one built into the app, never a path from the request
            assert status == 200 and body in (served[path], b"".join(_lines(6000)[-5:])), attempt
        elif path == "/monitor":
            assert status == 307
        else:
            assert status == 404, (attempt, status, body[:80])


def test_during_every_request_only_the_page_and_the_trace_file_are_ever_opened(site, monkeypatch):
    app, traces, page, _ = site
    opened = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    client = _client(app)
    for attempt in ATTEMPTS + ["/monitor/", "/monitor/api/traces", "/monitor/api/traces?limit=3", "/monitor"]:
        _raw_get(app, attempt)
        client.get(attempt) if "%00" not in attempt else None
    assert set(opened) <= {str(traces), str(page)}, set(opened) - {str(traces), str(page)}
    assert str(traces) in opened and str(page) in opened


def test_the_default_paths_are_the_page_in_dashboard_and_the_trace_file_of_the_tracer():
    assert DASHBOARD_PAGE == REPO_ROOT / "dashboard" / "index.html" and monitor.TRACE_PATH == TRACE_PATH
    assert TRACE_PATH == REPO_ROOT / "logs" / "traces.jsonl"


# --- loopback only: the middleware ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.255.255.254", "::1", "::ffff:127.0.0.1"])
def test_loopback_addresses_are_loopback(host):
    assert is_loopback_ip(host) and is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.2", "172.17.0.2", "8.8.8.8", "169.254.1.1", "2001:db8::1",
                                  "fe80::1", "::ffff:192.168.1.10", "::ffff:0.0.0.0", "example.com", "testclient", "", " 127.0.0.1",
                                  "127.0.0.1 ", "127.0.0.1.evil.com", "localhost.evil.com", "0x7f.0.0.1", "2130706433", "127.1", None])
def test_nothing_else_is_loopback(host):
    assert not is_loopback_ip(host) and not is_loopback_host(host)


def test_localhost_is_a_host_the_server_may_bind_but_never_the_address_of_a_peer():
    assert is_loopback_host("localhost") and not is_loopback_ip("localhost")
    assert not is_loopback_host("LOCALHOST.") and not is_loopback_host("localhost ")


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_requests_from_a_loopback_address_are_served(site, address):
    app, *_ = site
    assert _client(app, client=(address, 51000)).get("/monitor/api/traces?limit=1").status_code == 200


@pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.10", "10.0.0.2", "172.17.0.2", "8.8.8.8", "2001:db8::1", "::ffff:192.168.1.10",
                                     "testclient", "localhost", "127.0.0.1.evil.com", ""])
def test_a_request_from_a_non_loopback_address_is_403_on_every_route(site, address):
    app, *_ = site
    client = _client(app, client=(address, 51000))
    for path in ("/monitor/", "/monitor", "/monitor/api/traces", "/monitor/api/traces?limit=1", "/monitor/api/traces?limit=0",
                 "/nothing", "/docs", "/openapi.json", "/.env", "/monitor/../.env"):
        response = client.get(path)
        assert response.status_code == 403, (address, path)
        assert b"TOP-SECRET" not in response.content and b"stand-in" not in response.content and b'"i"' not in response.content
        assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"
    for method in ("post", "put", "delete", "options", "head"):
        assert getattr(client, method)("/monitor/api/traces").status_code == 403, method


def test_a_request_without_a_client_address_is_refused_not_trusted(site):
    app, *_ = site
    assert _raw_get(app, "/monitor/api/traces", client=None)[0] == 403


def test_forwarding_headers_are_never_believed(site):
    app, *_ = site
    spoof = (("x-forwarded-for", "127.0.0.1"), ("x-real-ip", "127.0.0.1"), ("forwarded", "for=127.0.0.1"), ("host", "127.0.0.1:8765"))
    assert _raw_get(app, "/monitor/api/traces", client=("203.0.113.9", 4000), headers=spoof)[0] == 403
    assert _raw_get(app, "/monitor/api/traces", client=("127.0.0.1", 4000), headers=(("x-forwarded-for", "203.0.113.9"),))[0] == 200  # nor held against


def test_the_guard_also_closes_a_websocket_from_outside_and_ignores_lifespan(site):
    app, *_ = site
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/monitor/api/traces", "headers": [], "client": ("203.0.113.9", 1), "server": ("127.0.0.1", 8765)}
    asyncio.run(app(scope, receive, send))
    assert sent and sent[0]["type"] in ("websocket.close", "websocket.http.response.start") and "accept" not in {m["type"] for m in sent}


def test_no_token_mode_exists_a_token_in_the_environment_changes_nothing(site, monkeypatch):
    app, *_ = site
    monkeypatch.setenv("MONITOR_TOKEN", "hunter2-hunter2")
    client = _client(app)
    assert client.get("/monitor/api/traces?limit=1").status_code == 200  # no Authorization header needed
    assert client.get("/monitor/api/traces?limit=1", headers={"Authorization": "Bearer wrong"}).status_code == 200
    outside = _client(app, client=("203.0.113.9", 1)).get("/monitor/api/traces?limit=1", headers={"Authorization": "Bearer hunter2-hunter2"})
    assert outside.status_code == 403  # a token does not open the door from outside either
    assert "MONITOR_TOKEN" not in Path(monitor.__file__).read_text(encoding="utf-8")  # nothing reads such a setting


# --- starting it ----------------------------------------------------------------------------------------------------------------


NO_ENV = Path("/nonexistent/.env")


def test_the_default_bind_is_127_0_0_1_port_8765(monkeypatch):
    for name in ("MONITOR_HOST", "MONITOR_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert parse_bind([], env_file=NO_ENV) == ("127.0.0.1", 8765)
    config = make_config("127.0.0.1", 8765)
    assert (config.host, config.port, config.proxy_headers) == ("127.0.0.1", 8765, False)


@pytest.mark.parametrize("argv", [["--host", "0.0.0.0"], ["--host", "::"], ["--host", "192.168.1.10"], ["--host", "10.0.0.2"],
                                  ["--host", "example.com"], ["--host", "127.0.0.1.evil.com"], ["--host", ""], ["--host", "0"],
                                  ["--host", "8.8.8.8", "--port", "8765"]])
def test_a_non_loopback_host_is_refused_before_anything_starts(argv, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: started.append(self.config))
    monkeypatch.delenv("MONITOR_HOST", raising=False)
    with pytest.raises(SystemExit) as refused:
        monitor.main(argv)
    assert refused.value.code not in (0, None) and started == []
    err = capsys.readouterr().err
    assert "loopback" in err and "reverse proxy" in err


@pytest.mark.parametrize("env", [{"MONITOR_HOST": "0.0.0.0"}, {"MONITOR_HOST": "0.0.0.0", "MONITOR_TOKEN": "hunter2-hunter2"},
                                 {"MONITOR_HOST": "192.168.1.10", "MONITOR_TOKEN": "x" * 40}])
def test_a_non_loopback_host_from_the_environment_is_refused_and_a_token_does_not_change_that(env, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    started = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: started.append(self.config))
    with pytest.raises(SystemExit) as refused:
        monitor.main([])
    assert refused.value.code not in (0, None) and started == []


def test_a_non_loopback_host_in_the_env_file_is_refused_too(tmp_path, monkeypatch):
    monkeypatch.delenv("MONITOR_HOST", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MONITOR_HOST=0.0.0.0\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        parse_bind([], env_file=env_file)


@pytest.mark.parametrize("argv, expected", [([], ("127.0.0.1", 8765)), (["--host", "localhost"], ("localhost", 8765)),
                                            (["--host", "::1", "--port", "9100"], ("::1", 9100)), (["--port", "1"], ("127.0.0.1", 1)),
                                            (["--host", "127.0.0.2", "--port", "65535"], ("127.0.0.2", 65535))])
def test_loopback_hosts_and_valid_ports_are_accepted(argv, expected, monkeypatch):
    monkeypatch.delenv("MONITOR_HOST", raising=False), monkeypatch.delenv("MONITOR_PORT", raising=False)
    assert parse_bind(argv, env_file=NO_ENV) == expected


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "80.5", ""])
def test_an_invalid_port_is_refused(port, monkeypatch):
    monkeypatch.delenv("MONITOR_HOST", raising=False)
    with pytest.raises(SystemExit):
        parse_bind(["--port", port], env_file=NO_ENV)


def test_main_runs_one_uvicorn_server_on_the_checked_host(monkeypatch):
    seen = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: seen.append((self.config.host, self.config.port, self.config.app)))
    for name in ("MONITOR_HOST", "MONITOR_PORT"):
        monkeypatch.delenv(name, raising=False)
    monitor.main([])
    assert seen == [("127.0.0.1", 8765, monitor.app)]


class RunningServer:
    """A real uvicorn server in a thread (port 0 = any free port)."""

    def __init__(self, config: uvicorn.Config) -> None:
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started, "the server did not start"
        self.sockets = [s for srv in self.server.servers for s in srv.sockets]
        return self

    @property
    def port(self) -> int:
        return self.sockets[0].getsockname()[1]

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(15)


def _lan_ip() -> str | None:
    """An address of this machine that is not loopback (None when there is none)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # a UDP connect sends nothing; it only picks the outgoing interface
        ip = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def test_the_default_server_really_listens_on_127_0_0_1_only(tmp_path):
    traces = _write(tmp_path / "logs" / "traces.jsonl", _lines(3))
    host, _ = parse_bind([], env_file=NO_ENV)
    config = make_config(host, 0, app=create_app(traces_path=traces, page_path=_write(tmp_path / "p.html", [PAGE])))
    with RunningServer(config) as running:
        assert [s.getsockname()[0] for s in running.sockets] == ["127.0.0.1"]
        assert httpx.get(f"http://127.0.0.1:{running.port}/monitor/api/traces").content == b"".join(_lines(3))
        lan = _lan_ip()
        if lan is not None:
            with pytest.raises(httpx.ConnectError):
                httpx.get(f"http://{lan}:{running.port}/monitor/api/traces", timeout=3)  # not reachable through the machine's other address


def test_even_uvicorn_on_0_0_0_0_cannot_expose_the_page_or_the_traces(tmp_path):
    lan = _lan_ip()
    if lan is None:
        pytest.skip("this machine has no non-loopback IPv4 address to connect from")
    traces = _write(tmp_path / "logs" / "traces.jsonl", _lines(3))
    app = create_app(traces_path=traces, page_path=_write(tmp_path / "p.html", [PAGE]))
    with RunningServer(uvicorn.Config(app, host="0.0.0.0", port=0, log_level="warning")) as running:  # what `uvicorn --host 0.0.0.0` does
        assert running.sockets[0].getsockname()[0] == "0.0.0.0"
        for path in ("/monitor/", "/monitor/api/traces", "/monitor"):
            denied = httpx.get(f"http://{lan}:{running.port}{path}", follow_redirects=False)
            assert denied.status_code == 403 and PAGE not in denied.content and b'"i"' not in denied.content, path
            spoofed = httpx.get(f"http://{lan}:{running.port}{path}", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
                                follow_redirects=False)
            assert spoofed.status_code == 403, path  # uvicorn trusts forwarding headers only from 127.0.0.1, and the app never does
        assert httpx.get(f"http://127.0.0.1:{running.port}/monitor/api/traces").status_code == 200  # the local machine still can

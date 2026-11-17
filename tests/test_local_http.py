"""Local service requests bypass proxies without weakening remote HTTP clients."""

import contextlib
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from live_translation import local_http


@contextlib.contextmanager
def server(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)


class Handler(BaseHTTPRequestHandler):
    visited = []

    def log_message(self, format, *args):
        return

    def do_GET(self):
        self.visited.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ready")
            self.end_headers()
            return
        if self.path.startswith("/to/"):
            self.send_response(302)
            self.send_header("Location", self.path.removeprefix("/to/"))
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body + b"|" + self.headers.get("Authorization", "").encode())


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234/",
        "http://example.com/",
        "http://127.0.0.1.evil.invalid/",
        "http://127.0.0.2/",
        "http://user:password@localhost/",
        "http://localhost:0/",
        "http://localhost:65536/",
        "http://localhost:not-a-port/",
        "file:///tmp/private",
        "http://localhost/#fragment",
        "http://local\nhost/",
    ],
)
def test_non_loopback_or_ambiguous_requests_are_rejected_before_opening(url):
    with (
        mock.patch.object(local_http.urllib.request, "build_opener") as opener,
        pytest.raises(ValueError),
    ):
        local_http.local_urlopen(url)
    opener.assert_not_called()


def test_loopback_request_bypasses_invalid_environment_and_macos_system_proxy(monkeypatch):
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    with (
        server(Handler) as url,
        mock.patch.object(
            urllib.request, "getproxies", return_value={"http": "http://127.0.0.1:1"}
        ),
        local_http.local_urlopen(url + "/ready", timeout=3) as response,
    ):
        assert response.status == 200
        assert response.read() == b'{"status":"ok"}'


def test_same_origin_relative_redirect_is_allowed():
    with server(Handler) as url, local_http.local_urlopen(url + "/redirect", timeout=3) as response:
        assert response.read() == b'{"status":"ok"}'


def test_redirect_to_another_local_service_is_blocked_before_the_second_service_is_called():
    class Target(Handler):
        visited = []

    with server(Target) as target, server(Handler) as origin:
        Target.visited.clear()
        with pytest.raises(urllib.error.HTTPError, match="same loopback origin"):
            local_http.local_urlopen(origin + "/to/" + target + "/private", timeout=3)
        assert Target.visited == []


def test_redirect_to_remote_origin_is_blocked():
    with (
        server(Handler) as origin,
        pytest.raises(urllib.error.HTTPError, match="same loopback origin"),
    ):
        local_http.local_urlopen(origin + "/to/https://example.com/private", timeout=3)


def test_request_post_body_and_authorization_are_preserved():
    with server(Handler) as url:
        request = urllib.request.Request(
            url + "/state", data=b"fixture", headers={"Authorization": "Bearer fixture-token"}
        )
        with local_http.local_urlopen(request, timeout=3) as response:
            assert response.read() == b"fixture|Bearer fixture-token"


def test_global_opener_proxy_discovery_and_environment_are_not_mutated(monkeypatch):
    original = urllib.request._opener
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    with server(Handler) as url, local_http.local_urlopen(url, timeout=3) as response:
        assert response.status == 200
    assert urllib.request._opener is original
    import os

    assert os.environ["HTTP_PROXY"] == "http://proxy.invalid:8080"


@pytest.mark.parametrize(
    "url", ["http://localhost", "http://127.0.0.1:1234/state", "http://[::1]:8080/health"]
)
def test_only_explicit_loopback_host_origins_are_accepted(url):
    assert local_http._origin(url)[1] in local_http.LOOPBACK_HOSTS

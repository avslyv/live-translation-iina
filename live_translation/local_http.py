"""Direct HTTP access to local services without inheriting system proxy routing.

Only the explicitly listed loopback hosts are accepted. The opener is private to
one call, and redirects must keep the original HTTP origin. Remote model and
package downloads retain their own HTTPS/proxy configuration.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _origin(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("Local HTTP requests require a valid loopback URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Local HTTP requests require a valid loopback URL") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Local HTTP requests accept only explicit HTTP loopback origins")
    return parsed.scheme, parsed.hostname, port or 80


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: tuple[str, str, int]):
        super().__init__()
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            redirected = _origin(urljoin(req.full_url, newurl))
            if redirected != self.origin:
                raise ValueError("Changed local origin")
        except ValueError as error:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Local HTTP redirects must stay at the same loopback origin",
                headers,
                fp,
            ) from error
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def local_urlopen(url: str | urllib.request.Request, timeout: float = 30):
    """Open one validated loopback request directly, never through an HTTP proxy."""
    destination = url.full_url if isinstance(url, urllib.request.Request) else url
    origin = _origin(destination)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SameOriginRedirect(origin),
    )
    return opener.open(url, timeout=timeout)

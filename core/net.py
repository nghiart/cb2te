"""Shared HTTP transport for Chaturbate probing and HLS ingest.

Chaturbate's edge/WAF rejects requests that do not look like a desktop browser
session, and the HLS playlists/segments are bound to the *room* (``Referer``)
and, in practice, to a residential/granted IP.  Bare ``urllib`` calls with a
mobile User-Agent and no Referer are routinely answered with ``403`` or with a
``room_status=public`` payload that has **no** ``hls_source`` (geo block).

This module centralises:

* Browser-like request headers (desktop UA, ``Referer``, ``Origin``, cookie).
* Proxy support including SOCKS5 (``urllib`` alone cannot speak SOCKS).
* Explicit status codes instead of silently swallowed exceptions.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_UA",
    "HttpResult",
    "NetError",
    "ProxyConfigError",
    "chaturbate_headers",
    "build_opener",
    "http_request",
    "normalize_proxy",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

CHATURBATE_ORIGIN = "https://chaturbate.com"

_SOCKS_SCHEMES = {"socks5", "socks5h", "socks4", "socks4a"}
_DEFAULT_PORTS = {"http": 8080, "https": 443, "socks5": 1080, "socks5h": 1080,
                  "socks4": 1080, "socks4a": 1080}


class NetError(Exception):
    """Transport failure (DNS, TLS, connection reset, proxy rejected...)."""


class ProxyConfigError(NetError):
    """The configured proxy cannot be used (bad URL or missing dependency)."""


@dataclass
class HttpResult:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def chaturbate_headers(
    room_id: str = "",
    *,
    html: bool = False,
    referer: str = "",
    csrf_token: str = "",
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build a browser-like Chaturbate header set.

    ``html=False`` produces the AJAX flavour used by
    ``/api/chatvideocontext/<room>/`` and ``/get_edge_hls_url_ajax/``.
    ``html=True`` produces the navigation flavour used for the room page.
    """
    ref = referer or (f"{CHATURBATE_ORIGIN}/{room_id}/" if room_id else f"{CHATURBATE_ORIGIN}/")
    headers: dict[str, str] = {
        "User-Agent": DEFAULT_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            if html
            else "application/json, text/javascript, */*; q=0.01"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": ref,
        "Origin": CHATURBATE_ORIGIN,
        "Cookie": f"agreeterms=1; csrftoken={csrf_token}" if csrf_token else "agreeterms=1",
    }
    if not html:
        headers["X-Requested-With"] = "XMLHttpRequest"
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token
    if extra:
        headers.update({k: v for k, v in extra.items() if v is not None})
    return headers


# ---------------------------------------------------------------------------
# Proxy parsing / opener
# ---------------------------------------------------------------------------

@dataclass
class ProxyInfo:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    raw: str = ""
    rdns: bool = True


def normalize_proxy(proxy: str) -> Optional[ProxyInfo]:
    """Parse ``http://``, ``https://``, ``socks5://``, ``socks5h://``...

    A bare ``host:port`` value is treated as an HTTP proxy (backwards compatible
    with the previous behaviour documented in ``settings.example.txt``).
    Returns ``None`` for an empty value and raises :class:`ProxyConfigError`
    for an unusable value.
    """
    raw = (proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    if not host:
        raise ProxyConfigError(f"Invalid proxy URL (no host): {proxy!r}")
    port = parsed.port or _DEFAULT_PORTS.get(scheme)
    if not port:
        raise ProxyConfigError(f"Invalid proxy URL (no port): {proxy!r}")
    return ProxyInfo(
        scheme=scheme,
        host=host,
        port=int(port),
        username=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
        raw=raw,
        rdns=scheme.endswith("h") or scheme == "socks5",
    )


def _build_socks_handler(info: ProxyInfo):
    """Create a urllib handler for a SOCKS proxy using PySocks."""
    try:
        import socks  # type: ignore
        from sockshandler import SocksiPyHandler  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on device
        raise ProxyConfigError(
            "SOCKS proxy requires PySocks. Install it with: pip install pysocks"
        ) from exc

    proxy_type = socks.SOCKS4 if info.scheme.startswith("socks4") else socks.SOCKS5
    kwargs: dict[str, Any] = {"rdns": info.rdns}
    if info.username:
        kwargs["username"] = info.username
        kwargs["password"] = info.password
    return SocksiPyHandler(proxy_type, info.host, info.port, **kwargs)


def build_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """Build an opener honouring HTTP(S) and SOCKS proxies."""
    handlers: list[urllib.request.BaseHandler] = []
    info = normalize_proxy(proxy)
    if info is not None:
        if info.scheme in _SOCKS_SCHEMES:
            handlers.append(_build_socks_handler(info))
        else:
            handlers.append(
                urllib.request.ProxyHandler({"http": info.raw, "https": info.raw})
            )
    return urllib.request.build_opener(*handlers)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _safe_status(resp: Any) -> int:
    """Read ``resp.status``/``resp.code`` defensively.

    Only real integers are accepted: test doubles (``MagicMock``) expose
    ``__int__`` and would otherwise be coerced to a bogus status.
    """
    for attr in ("status", "code"):
        try:
            value = getattr(resp, attr, None)
        except Exception:  # pragma: no cover - defensive
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return int(value)
    return 200


def _safe_url(resp: Any, fallback: str) -> str:
    try:
        value = resp.geturl()
        if isinstance(value, str) and value:
            return value
    except Exception:  # pragma: no cover - defensive
        pass
    return fallback


def _safe_headers(resp: Any) -> dict[str, str]:
    try:
        raw = resp.headers
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:  # pragma: no cover - defensive
        return {}


def http_request(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    proxy: str = "",
    timeout: float = 15.0,
    method: str = "GET",
    data: Optional[bytes] = None,
) -> HttpResult:
    """Perform one HTTP request.

    HTTP error statuses (>=400) are returned as :class:`HttpResult` so callers
    can distinguish "room offline" from "blocked by the edge" instead of
    treating every failure as offline.  Transport errors raise
    :class:`NetError`.
    """
    opener = build_opener(proxy)
    request = urllib.request.Request(
        url,
        data=data,
        headers=dict(headers or {}),
        method=method.upper(),
    )
    try:
        with opener.open(request, timeout=timeout) as resp:
            return HttpResult(
                status=_safe_status(resp),
                url=_safe_url(resp, url),
                headers=_safe_headers(resp),
                body=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read() or b""
        except Exception:  # pragma: no cover - defensive
            body = b""
        return HttpResult(
            status=int(getattr(exc, "code", 0) or 0),
            url=url,
            headers=_safe_headers(exc),
            body=body,
        )
    except urllib.error.URLError as exc:
        raise NetError(str(getattr(exc, "reason", exc))) from exc
    except (OSError, ValueError) as exc:
        raise NetError(str(exc)) from exc

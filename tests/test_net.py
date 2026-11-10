"""Tests for the HTTP transport, proxy parsing and Chaturbate request headers."""

import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from core.net import (
    DEFAULT_UA,
    HttpResult,
    NetError,
    ProxyConfigError,
    build_opener,
    chaturbate_headers,
    http_request,
    normalize_proxy,
)


class TestHeaders(unittest.TestCase):
    def test_ajax_headers_are_browser_like(self):
        headers = chaturbate_headers("some_room")
        self.assertEqual(headers["Referer"], "https://chaturbate.com/some_room/")
        self.assertEqual(headers["Origin"], "https://chaturbate.com")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(headers["User-Agent"], DEFAULT_UA)
        self.assertIn("agreeterms=1", headers["Cookie"])

    def test_html_mode_omits_xhr_header(self):
        headers = chaturbate_headers("room", html=True)
        self.assertNotIn("X-Requested-With", headers)
        self.assertIn("text/html", headers["Accept"])

    def test_csrf_token_added_to_cookie_and_header(self):
        headers = chaturbate_headers("room", csrf_token="ABC123")
        self.assertIn("csrftoken=ABC123", headers["Cookie"])
        self.assertEqual(headers["X-CSRFToken"], "ABC123")


class TestProxyParsing(unittest.TestCase):
    def test_empty_proxy(self):
        self.assertIsNone(normalize_proxy(""))
        self.assertIsNone(normalize_proxy("   "))

    def test_bare_host_port_is_http(self):
        info = normalize_proxy("127.0.0.1:8080")
        self.assertEqual(info.scheme, "http")
        self.assertEqual(info.host, "127.0.0.1")
        self.assertEqual(info.port, 8080)

    def test_socks5_with_credentials(self):
        info = normalize_proxy("socks5://user:pass@10.0.0.2:1080")
        self.assertEqual(info.scheme, "socks5")
        self.assertEqual(info.username, "user")
        self.assertTrue(info.rdns)

    def test_http_proxy_handler_receives_original_url(self):
        with patch("core.net.urllib.request.ProxyHandler") as handler, patch(
            "core.net.urllib.request.build_opener"
        ) as builder:
            build_opener("http://127.0.0.1:8080")
            handler.assert_called_once_with(
                {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
            )
            builder.assert_called_once()

    def test_invalid_proxy_raises(self):
        with self.assertRaises(ProxyConfigError):
            normalize_proxy("http://")

    def test_socks_without_pysocks_raises_helpful_error(self):
        with patch.dict("sys.modules", {"socks": None}):
            with self.assertRaises(ProxyConfigError) as ctx:
                build_opener("socks5://127.0.0.1:9050")
            self.assertIn("pysocks", str(ctx.exception).lower())


class TestHttpRequest(unittest.TestCase):
    @patch("core.net.urllib.request.build_opener")
    def test_successful_response(self, mock_build):
        resp = MagicMock()
        resp.status = 200
        resp.geturl.return_value = "https://chaturbate.com/api/chatvideocontext/x/"
        resp.headers = {"Content-Type": "application/json"}
        resp.read.return_value = b'{"room_status": "public"}'
        resp.__enter__.return_value = resp
        mock_build.return_value.open.return_value = resp

        result = http_request("https://chaturbate.com/api/chatvideocontext/x/")
        self.assertIsInstance(result, HttpResult)
        self.assertEqual(result.status, 200)
        self.assertTrue(result.ok)
        self.assertEqual(result.json()["room_status"], "public")

    @patch("core.net.urllib.request.build_opener")
    def test_http_error_is_returned_not_raised(self, mock_build):
        error = urllib.error.HTTPError(
            "https://chaturbate.com/x", 403, "Forbidden", {}, None
        )
        mock_build.return_value.open.side_effect = error

        result = http_request("https://chaturbate.com/x")
        self.assertEqual(result.status, 403)
        self.assertFalse(result.ok)

    @patch("core.net.urllib.request.build_opener")
    def test_transport_error_raises_net_error(self, mock_build):
        mock_build.return_value.open.side_effect = urllib.error.URLError("no route")
        with self.assertRaises(NetError):
            http_request("https://chaturbate.com/x")

    @patch("core.net.urllib.request.build_opener")
    def test_non_json_body_returns_none(self, mock_build):
        resp = MagicMock()
        resp.status = 200
        resp.geturl.return_value = "https://chaturbate.com/x"
        resp.headers = {}
        resp.read.return_value = b"<html>Cloudflare</html>"
        resp.__enter__.return_value = resp
        mock_build.return_value.open.return_value = resp

        result = http_request("https://chaturbate.com/x")
        self.assertIsNone(result.json())


if __name__ == "__main__":
    unittest.main()

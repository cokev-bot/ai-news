"""Tests for the Phase 4 feed-fetching resilience layer.

Covers `_looks_like_rss` (body shape check), `_http_get_with_retry` (retry
+ body validation), and `fetch_feed` (fallback chain behavior). Uses a
local HTTP server so we don't depend on nitter being up during tests.
"""
import http.server
import socketserver
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from generate_news import _looks_like_rss, _http_get_with_retry, fetch_feed


def _recent_rss() -> bytes:
    """Build a valid RSS fixture with a pubDate that is always within
    MAX_AGE_DAYS (7) so the age filter in fetch_feed never drops it."""
    pub = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<link>http://example.com</link>
<description>Test</description>
<item>
  <title>Test Item One</title>
  <link>http://example.com/a</link>
  <description>First item</description>
  <pubDate>{pub}</pubDate>
</item>
</channel></rss>""".encode()


# Kept as a module-level constant for the _looks_like_rss tests that only
# need a body with the right XML shape (age filtering is irrelevant there).
VALID_RSS = _recent_rss()

EMPTY_BODY = b""
HTML_ERROR = b"<html><body>503 Service Unavailable</body></html>"


def make_handler(responses):
    """Build a request handler that returns the i-th response for the i-th
    request. `responses` is a list of (status, body, content_type) tuples.
    After the list is exhausted, returns 200 + EMPTY_BODY."""
    state = {"call_count": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):  # silence stderr noise
            pass

        def do_GET(self):
            i = state["call_count"]
            state["call_count"] += 1
            if i < len(responses):
                status, body, ctype = responses[i]
            else:
                status, body, ctype = 200, EMPTY_BODY, "application/rss+xml"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler, state


def serve_responses(responses):
    """Spin up an HTTP server on a free port serving the given response list.
    Returns (port, call_counter, server_thread). Caller must call .server.shutdown()."""
    Handler, state = make_handler(responses)

    # Bind to port 0 to get a free port
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True
        def __init__(self, addr, h):
            super().__init__(addr, h)

    server = ReusableTCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, state, server


class TestLooksLikeRss(unittest.TestCase):

    def test_valid_rss(self):
        self.assertTrue(_looks_like_rss(VALID_RSS))

    def test_empty_body(self):
        self.assertFalse(_looks_like_rss(EMPTY_BODY))

    def test_too_short(self):
        self.assertFalse(_looks_like_rss(b"<rss"))

    def test_just_under_threshold(self):
        # 50 bytes is below the 100-byte floor, so we reject even if it has
        # the right shape
        self.assertFalse(_looks_like_rss(b"<rss version='2.0'><channel><title>x</title></channel></rss>"))

    def test_html_error_page(self):
        # Some upstreams return 200 with an HTML error body; that is NOT an RSS
        self.assertFalse(_looks_like_rss(HTML_ERROR))

    def test_atom_feed(self):
        # Atom root element must be detected even when preceded by xml decl.
        # Padded so it's over the 100-byte floor.
        atom = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title><subtitle>hello</subtitle></feed>'
        self.assertTrue(_looks_like_rss(atom))

    def test_none(self):
        self.assertFalse(_looks_like_rss(None))  # type: ignore[arg-type]


class TestHttpGetWithRetry(unittest.TestCase):
    def setUp(self):
        self._servers = []

    def tearDown(self):
        for s in self._servers:
            s.shutdown()
            s.server_close()

    def _serve(self, responses):
        port, state, server = serve_responses(responses)
        self._servers.append(server)
        return port, state

    def test_succeeds_first_try(self):
        port, state = self._serve([(200, VALID_RSS, "application/rss+xml")])
        result = _http_get_with_retry(f"http://127.0.0.1:{port}/", attempts=3, backoff_base=0.0)
        self.assertEqual(result, VALID_RSS)

    def test_retries_on_empty_body(self):
        # First two attempts return empty bodies (nitter's rate-limit failure
        # mode), third attempt returns a valid RSS. The function should keep
        # trying and eventually succeed.
        port, state = self._serve([
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, VALID_RSS, "application/rss+xml"),
        ])
        t0 = time.time()
        result = _http_get_with_retry(f"http://127.0.0.1:{port}/", attempts=3, backoff_base=0.0)
        elapsed = time.time() - t0
        self.assertEqual(result, VALID_RSS)
        self.assertEqual(state["call_count"], 3)
        # With backoff_base=0, total wall time should be tiny
        self.assertLess(elapsed, 2.0)

    def test_returns_none_after_all_attempts_fail(self):
        port, state = self._serve([(200, EMPTY_BODY, "application/rss+xml")] * 5)
        result = _http_get_with_retry(f"http://127.0.0.1:{port}/", attempts=3, backoff_base=0.0)
        self.assertIsNone(result)
        self.assertEqual(state["call_count"], 3)

    def test_returns_none_on_connection_refused(self):
        # Port 1 is privileged and (almost always) refused
        result = _http_get_with_retry("http://127.0.0.1:1/", attempts=2, timeout=1, backoff_base=0.0)
        self.assertIsNone(result)


class TestFetchFeedFallbackChain(unittest.TestCase):
    def setUp(self):
        self._servers = []

    def tearDown(self):
        for s in self._servers:
            s.shutdown()
            s.server_close()

    def _serve(self, responses):
        port, state, server = serve_responses(responses)
        self._servers.append(server)
        return port, state

    def test_primary_works_no_fallback_used(self):
        port, state = self._serve([(200, VALID_RSS, "application/rss+xml")])
        url = f"http://127.0.0.1:{port}/primary"
        alts = [f"http://127.0.0.1:{port}/fallback1"]
        articles = fetch_feed("TestFeed", url, fallbacks=alts)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "Test Item One")
        self.assertEqual(state["call_count"], 1)

    def test_falls_back_to_first_alt(self):
        port, state = self._serve([
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, VALID_RSS, "application/rss+xml"),
        ])
        url = f"http://127.0.0.1:{port}/primary"
        alts = [f"http://127.0.0.1:{port}/fallback1"]
        articles = fetch_feed("TestFeed", url, fallbacks=alts)
        self.assertEqual(len(articles), 1)
        self.assertEqual(state["call_count"], 4)

    def test_walks_through_multiple_fallbacks(self):
        port, state = self._serve([
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, EMPTY_BODY, "application/rss+xml"),
            (200, VALID_RSS, "application/rss+xml"),
        ])
        url = f"http://127.0.0.1:{port}/primary"
        alts = [f"http://127.0.0.1:{port}/alt1", f"http://127.0.0.1:{port}/alt2"]
        articles = fetch_feed("TestFeed", url, fallbacks=alts)
        self.assertEqual(len(articles), 1)

    def test_all_fails_returns_empty(self):
        port, state = self._serve([(200, EMPTY_BODY, "application/rss+xml")] * 20)
        url = f"http://127.0.0.1:{port}/primary"
        alts = [f"http://127.0.0.1:{port}/alt1"]
        articles = fetch_feed("TestFeed", url, fallbacks=alts)
        self.assertEqual(articles, [])


if __name__ == "__main__":
    unittest.main()

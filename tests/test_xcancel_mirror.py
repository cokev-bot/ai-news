"""Tests for the xcancel RSS mirror transport.

Context (verified 2026-09-13): `rss.xcancel.com/<acct>/rss` is a live mirror of
26 of the 34 configured feeds, but its behaviour is unusually hostile:

  * it serves a 1971-dated "RSS reader not yet whitelisted!" placeholder to
    almost every User-Agent — including the pipeline's own;
  * it prefixes its XML declaration with two whitespace bytes, so expat rejects
    the document unless it is lstripped;
  * it refuses real content to Python's TLS/HTTP stack (urllib, http.client,
    aiohttp, curl_cffi) while serving it to the `curl` binary for a byte-identical
    request, so the transport must be curl.

These tests pin all three behaviours plus the fallback chain, so a future
"cleanup" of the UA string or the parse call cannot silently re-break 26 feeds.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import generate_news  # noqa: E402
from generate_news import (  # noqa: E402
    XCANCEL_UA,
    _http_get_with_curl,
    _http_get_with_retry,
    _subsection_key,
    fetch_all_feeds,
    fetch_feed,
)

# A real xcancel body: two leading whitespace bytes before the declaration,
# exactly as the mirror emits it.
XCANCEL_BODY = (
    b'  <?xml version="1.0" encoding="UTF-8"?>\n'
    b'  <rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0"><channel>'
    b'<title>Anthropic (@AnthropicAI)</title>'
    b'<item><title>Real story</title><link>https://rss.xcancel.com/AnthropicAI/status/1#m</link>'
    b'<pubDate>DYNAMIC</pubDate></item>'
    b'</channel></rss>'
)

PLACEHOLDER_BODY = (
    b'  <?xml version="1.0" encoding="UTF-8"?>\n'
    b'  <rss version="2.0"><channel>'
    b'<title>RSS reader not yet whitelisted!</title>'
    b'<item><title>RSS reader not yet whitelisted!</title>'
    b'<pubDate>Mon, 01 January 1971 00:00:00 GMT</pubDate></item>'
    b'</channel></rss>'
)


def fresh_body() -> bytes:
    """XCANCEL_BODY with a pubDate inside the 7-day window."""
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000")
    return XCANCEL_BODY.replace(b"DYNAMIC", stamp.encode())


class TestXcancelUserAgent(unittest.TestCase):

    def test_ua_is_the_allowlisted_reader_identity(self):
        """The mirror only serves content to this reader string.

        Changing it (e.g. to the pipeline's own 'AI-News-Digest/1.1') silently
        returns every X feed to the 1971 placeholder.
        """
        self.assertEqual(XCANCEL_UA, "Inoreader/1.0")

    def test_xcancel_urls_are_routed_through_curl(self):
        """Python's HTTP stack gets the placeholder; curl gets real content."""
        with patch("generate_news._http_get_with_curl",
                   return_value=fresh_body()) as mock_curl, \
             patch("urllib.request.urlopen") as mock_urlopen:
            raw = _http_get_with_retry("https://rss.xcancel.com/AnthropicAI/rss")
            self.assertIsNotNone(raw)
            mock_curl.assert_called_once()
            mock_urlopen.assert_not_called()

    def test_non_xcancel_urls_still_use_urllib(self):
        # Must exceed the 100-byte floor in _looks_like_rss.
        body = (b'<?xml version="1.0"?><rss><channel><item><title>x</title>'
                b'<description>' + b'pad ' * 30 + b'</description>'
                b'</item></channel></rss>')
        self.assertTrue(generate_news._looks_like_rss(body), "fixture must pass the shape check")
        fake_resp = MagicMock()
        fake_resp.read.return_value = body
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: False
        with patch("generate_news._http_get_with_curl") as mock_curl, \
             patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            raw = _http_get_with_retry("https://example.com/feed.xml")
            self.assertIsNotNone(raw)
            mock_curl.assert_not_called()
            mock_open.assert_called_once()

    def test_curl_invocation_passes_the_allowlisted_ua(self):
        """The UA must actually reach curl's argv."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class R:
                stdout = fresh_body()
            return R()

        with patch("generate_news.subprocess.run", side_effect=fake_run):
            _http_get_with_curl("https://rss.xcancel.com/AnthropicAI/rss")
        self.assertIn("-A", captured["cmd"])
        self.assertIn(XCANCEL_UA, captured["cmd"])

    def test_curl_failure_returns_none_not_raises(self):
        with patch("generate_news.subprocess.run", side_effect=OSError("no curl")):
            self.assertIsNone(_http_get_with_curl("https://rss.xcancel.com/a/rss"))

    def test_curl_empty_body_returns_none(self):
        def fake_run(cmd, **kwargs):
            class R:
                stdout = b""
            return R()
        with patch("generate_news.subprocess.run", side_effect=fake_run):
            self.assertIsNone(_http_get_with_curl("https://rss.xcancel.com/a/rss"))


class TestXcancelWhitespaceParse(unittest.TestCase):
    """The mirror's leading whitespace must not zero out the X feed set."""

    def test_body_with_leading_whitespace_parses(self):
        with patch("generate_news._http_get_with_retry", return_value=fresh_body()):
            arts = fetch_feed("AnthropicAI", "https://rss.xcancel.com/AnthropicAI/rss")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["title"], "Real story")

    def test_without_lstrip_the_body_would_fail(self):
        """Guards the reason the lstrip exists — not just that it is there."""
        import xml.etree.ElementTree as ET
        with self.assertRaises(ET.ParseError):
            ET.fromstring(fresh_body())

    def test_placeholder_feed_yields_no_articles(self):
        """The 1971 placeholder must never surface as a story."""
        with patch("generate_news._http_get_with_retry", return_value=PLACEHOLDER_BODY):
            arts = fetch_feed("AnthropicAI", "https://rss.xcancel.com/AnthropicAI/rss")
        self.assertEqual(arts, [])


class TestXcancelFallbackChain(unittest.TestCase):
    """The legacy nitter hosts are dead; the chain must not mask the live URL."""

    def test_primary_xcancel_wins_over_dead_fallbacks(self):
        fb = ["https://nitter.privacydev.net/a/rss",
              "https://nitter.privacyredirect.com/a/rss"]
        with patch("generate_news._http_get_with_retry", return_value=fresh_body()):
            arts = fetch_feed("A", "https://rss.xcancel.com/a/rss", fallbacks=fb)
        self.assertEqual(len(arts), 1)

    def test_falls_back_when_primary_unusable(self):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return None if "rss.xcancel.com" in url else fresh_body()

        with patch("generate_news._http_get_with_retry", side_effect=fake), \
             patch("generate_news.time.sleep"):
            arts = fetch_feed("A", "https://rss.xcancel.com/a/rss",
                              fallbacks=["https://nitter.privacyredirect.com/a/rss"])
        self.assertEqual(len(arts), 1)
        self.assertEqual(len(calls), 2)


class TestSectionsJsonRepointed(unittest.TestCase):
    """Every X feed must point at the live mirror, keeping dead hosts as fallbacks."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((PROJECT_ROOT / "sections.json").read_text(encoding="utf-8"))

    def _feeds(self):
        for section in self.data["sections"]:
            for sub in section.get("subsections", []):
                for name, url in sub["feeds"].items():
                    yield sub, name, url

    def test_all_x_feeds_use_rss_xcancel(self):
        x = [(n, u) for _, n, u in self._feeds()
             if "/rss" in u and "http" in u and
             ("nitter" in u or "xcancel" in u)]
        # Not pinned to a count (26) — the user edits sections.json by hand,
        # and a count pin breaks on every X-feed add/remove. Assert the
        # property for every X feed instead, plus that at least one exists so
        # the check cannot pass vacuously.
        self.assertGreater(len(x), 0, "expected at least one X-mirror feed")
        for name, url in x:
            self.assertIn("rss.xcancel.com", url,
                          f"{name} still points at a dead host: {url}")

    def test_no_feed_points_at_a_bare_nitter_host(self):
        for _, name, url in self._feeds():
            self.assertNotIn("https://nitter.net/", url,
                             f"{name} still uses the dead primary nitter.net")

    def test_dead_hosts_retained_as_fallbacks_for_provenance(self):
        n = 0
        for section in self.data["sections"]:
            for sub in section.get("subsections", []):
                for name, alts in (sub.get("feeds_alts") or {}).items():
                    if name in sub["feeds"] and "rss.xcancel.com" in sub["feeds"][name]:
                        self.assertTrue(alts, f"{name} lost its fallback chain")
                        n += 1
        # The property is "every xcancel feed keeps a fallback chain"; assert
        # at least one such feed exists rather than pinning the count to 26.
        self.assertGreater(n, 0, "expected at least one xcancel feed with a fallback chain")

    def test_source_urls_unchanged_still_point_at_x(self):
        """Reader-facing links should stay x.com, not the mirror."""
        for name, url in self.data["source_urls"].items():
            self.assertNotIn("xcancel", url)
            self.assertNotIn("nitter", url)


class TestEndToEndRecovery(unittest.TestCase):
    """The whole point: X feeds must actually deliver articles to an edition."""

    def test_fetch_all_feeds_recovers_x_content(self):
        """With the curl transport, fetch_all_feeds pulls X articles again."""
        sections = [{
            "title": "Labs",
            "subsections": [{
                "title": "Anthropic",
                "feeds": {"AnthropicAI": "https://rss.xcancel.com/AnthropicAI/rss"},
            }],
        }]

        def fake_curl(url, **kwargs):
            return fresh_body() if "AnthropicAI" in url else None

        with patch("generate_news._http_get_with_curl", side_effect=fake_curl):
            res = fetch_all_feeds(sections, max_items_per_source=20, max_age_days=7)

        # Grouped by subsection POSITION, not title (titles repeat across
        # sections in sections.json, so a title can never be the key).
        arts = res[_subsection_key(0, 0)][0][1]
        self.assertEqual(len(arts), 1, "X feed produced no articles")


class TestCurlBinaryAvailable(unittest.TestCase):

    def test_curl_exists_on_path(self):
        """The transport shells out to curl; absence would break 26 feeds."""
        r = subprocess.run(["curl", "--version"], capture_output=True)
        self.assertEqual(r.returncode, 0, "curl must be installed for the xcancel transport")

    def test_missing_curl_logs_one_clear_error_and_degrades(self):
        """A missing curl must produce ONE diagnostic, not a silent 26-feed failure."""
        import generate_news as gn
        with patch.object(gn.shutil, "which", return_value=None), \
             patch.object(gn, "_CURL_AVAILABLE", None), \
             patch.object(gn, "subprocess") as mock_sub, \
             self.assertLogs("root", level="ERROR") as cm:
            result = gn._http_get_with_curl("https://rss.xcancel.com/a/rss")
        self.assertIsNone(result)
        mock_sub.run.assert_not_called()
        joined = " ".join(cm.output)
        self.assertIn("curl is not on PATH", joined)
        self.assertIn("X/Twitter", joined)
        # cache is populated so the error is logged only once per process
        self.assertFalse(gn._CURL_AVAILABLE)

    def test_curl_availability_is_cached(self):
        """Repeated calls must not re-run which() or re-log."""
        import generate_news as gn
        with patch.object(gn.shutil, "which", return_value="/usr/bin/curl") as m, \
             patch.object(gn, "_CURL_AVAILABLE", None):
            self.assertTrue(gn._curl_available())
            self.assertTrue(gn._curl_available())
            self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()

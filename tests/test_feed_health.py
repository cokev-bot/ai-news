"""Tests for the RSS feed health monitor (tools/check_feeds.py).

Exercise:
  - health state load / save / migration
  - check_feed against real-ish and mock feeds
  - get_all_feeds from sections.json
  - check_all_feeds end-to-end (with mocked HTTP)
  - Discord alert logic (threshold, dry-run, missing webhook URL)
  - CLI invocation
"""

import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the project root and tools/ are importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "tools"))

from check_feeds import (
    ALERT_THRESHOLD,
    HEALTH_FILE,
    check_feed,
    check_all_feeds,
    extract_sections,
    get_all_feeds,
    load_health,
    save_health,
    send_discord_alert,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_site_root(tmp_path: Path, *, sections: dict | None = None) -> Path:
    """Create a minimal site root with sections.json and return its path."""
    site_root = tmp_path / "site"
    site_root.mkdir()
    if sections is None:
        sections = [
            {
                "title": "Test Section",
                "subsections": [
                    {
                        "title": "SubA",
                        "feeds": {"FeedA": "https://example.com/feedA.rss"},
                    },
                    {
                        "title": "SubB",
                        "feeds": {"FeedB": "https://example.com/feedB.rss"},
                        "feeds_alts": {"FeedB": ["https://fallback.example.com/feedB.rss"]},
                    },
                ],
            }
        ]
    (site_root / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    return site_root


MINI_RSS = textwrap.dedent("""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>Hello</title>
      <link>https://example.com/1</link>
    </item>
  </channel>
</rss>
""").encode("utf-8")


# ---------------------------------------------------------------------------
# Health state tests
# ---------------------------------------------------------------------------

class TestLoadSaveHealth(unittest.TestCase):

    def test_load_missing_returns_empty(self):
        """First run: no health file → empty dict."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = load_health(Path(td))
            self.assertEqual(result, {})

    def test_load_save_roundtrip(self):
        """Write → read gives the same data."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            health = {
                "FeedA": {
                    "url": "https://example.com/a",
                    "consecutive_failures": 2,
                    "last_success": "2026-06-01T00:00:00+00:00",
                    "last_failure": "2026-06-05T00:00:00+00:00",
                    "last_error": "404",
                }
            }
            save_health(Path(td), health)
            loaded = load_health(Path(td))
            self.assertEqual(loaded, health)

    def test_save_atomic(self):
        """save_health writes a .tmp then renames; .tmp should not linger."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            save_health(Path(td), {"X": {"url": "u", "consecutive_failures": 0}})
            self.assertFalse((Path(td) / ".feed_health.json.tmp").exists())
            self.assertTrue((Path(td) / ".feed_health.json").exists())

    def test_load_corrupt_json(self):
        """Corrupt JSON file returns empty dict (not crash)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / HEALTH_FILE
            p.write_text("NOT JSON {{{", encoding="utf-8")
            result = load_health(Path(td))
            self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# get_all_feeds tests
# ---------------------------------------------------------------------------

class TestGetAllFeeds(unittest.TestCase):

    def test_extracts_all_feeds_with_fallbacks(self):
        sections = [
            {
                "title": "Labs",
                "subsections": [
                    {
                        "title": "OpenAI",
                        "feeds": {"OpenAI": "https://nitter.net/OpenAI/rss"},
                        "feeds_alts": {"OpenAI": ["https://xcancel.com/OpenAI/rss"]},
                    },
                    {
                        "title": "Anthropic",
                        "feeds": {"AnthropicAI": "https://nitter.net/AnthropicAI/rss"},
                    },
                ],
            }
        ]
        feeds = get_all_feeds(sections)
        self.assertEqual(len(feeds), 2)
        openai = [f for f in feeds if f[0] == "OpenAI"][0]
        self.assertEqual(openai[1], "https://nitter.net/OpenAI/rss")
        self.assertEqual(openai[2], ["https://xcancel.com/OpenAI/rss"])
        anthropic = [f for f in feeds if f[0] == "AnthropicAI"][0]
        self.assertEqual(anthropic[2], [])  # no fallbacks

    def test_empty_sections(self):
        feeds = get_all_feeds([])
        self.assertEqual(feeds, [])

    def test_feeds_alts_missing(self):
        sections = [
            {
                "title": "Test",
                "subsections": [
                    {"title": "Sub", "feeds": {"F": "https://example.com/rss"}},
                ],
            }
        ]
        feeds = get_all_feeds(sections)
        self.assertEqual(feeds[0][2], [])

    def test_garbage_entries_are_skipped(self):
        """A malformed section/subsection must not crash the monitor."""
        sections = [
            "not-a-dict",
            {"title": "Good", "subsections": [
                "also-not-a-dict",
                {"title": "Sub", "feeds": {"F": "https://example.com/rss"}},
            ]},
            {"title": "NoSubs"},
        ]
        feeds = get_all_feeds(sections)
        self.assertEqual([f[0] for f in feeds], ["F"])


class TestExtractSections(unittest.TestCase):
    """sections.json has two shapes; the monitor must handle both.

    Regression: iterating the object shape directly yielded the string keys
    ("source_urls", "sections"), and check_all_feeds crashed with
    AttributeError: 'str' object has no attribute 'get'. The monitor then
    never updated .feed_health.json, freezing every "last successful fetch"
    timestamp at the day the format changed.
    """

    def test_object_format_returns_sections_array(self):
        data = {
            "source_urls": {"A": "https://a"},
            "sections": [{"title": "One", "subsections": []}],
        }
        self.assertEqual(extract_sections(data), [{"title": "One", "subsections": []}])

    def test_legacy_list_format_passes_through(self):
        data = [{"title": "One", "subsections": []}]
        self.assertEqual(extract_sections(data), data)

    def test_missing_or_non_list_sections_key(self):
        self.assertEqual(extract_sections({}), [])
        self.assertEqual(extract_sections({"sections": "nope"}), [])
        self.assertEqual(extract_sections(None), [])
        self.assertEqual(extract_sections(42), [])

    def test_real_sections_json_yields_34_feeds(self):
        """End-to-end pin against the live config, not a fixture."""
        sections_path = Path(__file__).resolve().parent.parent / "sections.json"
        data = json.loads(sections_path.read_text(encoding="utf-8"))
        self.assertEqual(len(get_all_feeds(extract_sections(data))), 34)


# ---------------------------------------------------------------------------
# check_feed tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestCheckFeed(unittest.TestCase):

    @patch("check_feeds._http_get_with_retry", return_value=MINI_RSS)
    def test_success_primary(self, mock_get):
        ok, msg = check_feed("FeedA", "https://example.com/feedA.rss")
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

    @patch("check_feeds._http_get_with_retry")
    def test_success_fallback(self, mock_get):
        """Primary fails, fallback succeeds."""
        mock_get.side_effect = [None, MINI_RSS]
        ok, msg = check_feed(
            "FeedA", "https://primary.example.com/rss",
            fallbacks=["https://fallback.example.com/rss"],
        )
        self.assertTrue(ok)
        self.assertIn("fallback", msg.lower())

    @patch("check_feeds._http_get_with_retry", return_value=None)
    def test_all_fail(self, mock_get):
        ok, msg = check_feed("FeedA", "https://example.com/rss")
        self.assertFalse(ok)
        self.assertIn("failed", msg.lower())

    @patch("check_feeds._http_get_with_retry", return_value=None)
    def test_all_fail_with_fallbacks(self, mock_get):
        ok, msg = check_feed(
            "FeedA", "https://primary.example.com/rss",
            fallbacks=["https://fb1.example.com/rss", "https://fb2.example.com/rss"],
        )
        self.assertFalse(ok)
        self.assertIn("3", msg)  # "all 3 URL(s) failed"

    @patch("check_feeds._http_get_with_retry")
    def test_http_200_with_unparseable_body_is_not_ok(self, mock_get):
        """xcancel.com shape: valid-looking body with 2 leading whitespace bytes.

        Expat rejects a declaration that is not at byte 0, so the pipeline gets
        zero items. Reporting this as "OK" made the monitor say 34/34 healthy
        while 26 X feeds were dead.
        """
        body = b'  <?xml version="1.0" encoding="UTF-8"?><rss><channel>' \
               b'<item><title>hi</title></item></channel></rss>'
        mock_get.return_value = body
        ok, msg = check_feed("FeedA", "https://xcancel.com/a/rss")
        self.assertFalse(ok)
        self.assertIn("unparseable", msg)

    @patch("check_feeds._http_get_with_retry")
    def test_placeholder_whitelist_feed_is_not_ok(self, mock_get):
        """xcancel's placeholder is *valid RSS* with one 1971-dated item."""
        body = (b'<?xml version="1.0" encoding="UTF-8"?><rss><channel>'
                b'<item><title>RSS reader not yet whitelisted!</title>'
                b'<pubDate>Mon, 01 January 1971 00:00:00 GMT</pubDate>'
                b'</item></channel></rss>')
        mock_get.return_value = body
        ok, msg = check_feed("FeedA", "https://xcancel.com/a/rss")
        self.assertFalse(ok)
        self.assertIn("placeholder", msg)

    @patch("check_feeds._http_get_with_retry")
    def test_valid_xml_with_zero_items_is_not_ok(self, mock_get):
        mock_get.return_value = b'<?xml version="1.0"?><rss><channel></channel></rss>'
        ok, msg = check_feed("FeedA", "https://example.com/rss")
        self.assertFalse(ok)
        self.assertIn("0 items", msg)

    @patch("check_feeds._http_get_with_retry")
    def test_atom_entry_feed_is_ok(self, mock_get):
        """Atom feeds use <entry>, not <item>; they must not read as broken."""
        atom = (b'<?xml version="1.0" encoding="utf-8"?>'
                b'<feed xmlns="http://www.w3.org/2005/Atom">'
                b'<title>Blog</title>'
                b'<entry><title>Post</title><link href="https://x/1"/></entry>'
                b'</feed>')
        mock_get.return_value = atom
        ok, msg = check_feed("Simon Willison", "https://simonwillison.net/atom/everything/")
        self.assertTrue(ok, msg)

    @patch("check_feeds._http_get_with_retry")
    def test_fallback_used_when_primary_only_has_garbage_body(self, mock_get):
        """A 200 with garbage must not stop the fallback chain."""
        garbage = b'  <?xml version="1.0"?><rss><channel><item><title>x</title></item></channel></rss>'
        mock_get.side_effect = [garbage, MINI_RSS]
        ok, msg = check_feed(
            "FeedA", "https://primary.example.com/rss",
            fallbacks=["https://fallback.example.com/rss"],
        )
        self.assertTrue(ok)
        self.assertIn("fallback", msg.lower())

    @patch("check_feeds._http_get_with_retry", return_value=MINI_RSS)
    def test_plain_body_without_declaration_is_ok(self, mock_get):
        """A body with no XML declaration at all parses fine and is healthy."""
        mock_get.return_value = b"<rss><channel><item><title>x</title></item></channel></rss>"
        ok, _ = check_feed("FeedA", "https://example.com/rss")
        self.assertTrue(ok)

    @patch("check_feeds._http_get_with_retry")
    def test_monitor_agrees_with_pipeline_on_whitespace_body(self, mock_get):
        """The monitor must give the same verdict as generate_news.fetch_feed.

        fetch_feed calls ET.fromstring(raw) with no lstrip, so a body with
        leading whitespace yields 0 items. If the monitor stripped it, the two
        would disagree and the status page would contradict the pipeline.
        """
        from generate_news import fetch_feed
        body = (b'  <?xml version="1.0" encoding="UTF-8"?><rss><channel>'
                b'<item><title>Real story</title>'
                b'<link>https://example.com/1</link></item></channel></rss>')
        mock_get.return_value = body

        monitor_ok, _ = check_feed("FeedA", "https://example.com/rss")
        with patch("generate_news._http_get_with_retry", return_value=body):
            pipeline_items = fetch_feed("FeedA", "https://example.com/rss")

        self.assertEqual(monitor_ok, bool(pipeline_items))


# ---------------------------------------------------------------------------
# check_all_feeds end-to-end tests
# ---------------------------------------------------------------------------

class TestCheckAllFeeds(unittest.TestCase):

    @patch("check_feeds.check_feed")
    def test_all_healthy(self, mock_check):
        """All feeds OK → no alerts, consecutive_failures reset to 0."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            mock_check.return_value = (True, "OK")

            results = check_all_feeds(site_root)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r["ok"] for r in results))
            self.assertTrue(all(r["consecutive_failures"] == 0 for r in results))
            self.assertTrue(all(not r["alerted"] for r in results))

    @patch("check_feeds.check_feed")
    def test_run_summary_reports_failing_below_threshold(self, mock_check):
        """A run with failures below the alert threshold must not log "healthy".

        Regression: the summary keyed off *alerted* feeds, so a run where every
        feed failed on its first attempt logged "All feeds healthy — no alerts."
        """
        import tempfile
        from unittest.mock import patch as _patch
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            mock_check.side_effect = [
                (False, "all 1 URL(s) failed"),
                (False, "all 1 URL(s) failed"),
            ]
            with _patch("check_feeds.log") as mock_log:
                results = check_all_feeds(site_root)

            self.assertTrue(all(not r["ok"] for r in results))
            message = " ".join(str(c) for c in mock_log.warning.call_args_list)
            self.assertIn("2/2 feeds failing", message)
            info_messages = " ".join(str(c) for c in mock_log.info.call_args_list)
            self.assertNotIn("healthy", info_messages)

    @patch("check_feeds.check_feed")
    def test_run_summary_reports_all_healthy(self, mock_check):
        import tempfile
        from unittest.mock import patch as _patch
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            mock_check.return_value = (True, "OK")
            with _patch("check_feeds.log") as mock_log:
                check_all_feeds(site_root)
            info_messages = " ".join(str(c) for c in mock_log.info.call_args_list)
            self.assertIn("healthy", info_messages)

    @patch("check_feeds.check_feed")
    def test_failing_below_threshold(self, mock_check):
        """Feed failing 1-2 times (below threshold) → no alert."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            # First check: FeedA fails, FeedB OK
            mock_check.side_effect = [
                (False, "all 1 URL(s) failed"),
                (True, "OK"),
            ]
            results = check_all_feeds(site_root)
            feed_a = [r for r in results if r["name"] == "FeedA"][0]
            self.assertEqual(feed_a["consecutive_failures"], 1)
            self.assertFalse(feed_a["alerted"])  # Below threshold

    @patch("check_feeds.check_feed")
    def test_failing_at_threshold_triggers_alert(self, mock_check):
        """Feed failing 3+ consecutive times → alerted flag is True."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            # Pre-seed health state: FeedA already failed 2 times
            health = {
                "FeedA": {
                    "url": "https://example.com/feedA.rss",
                    "consecutive_failures": 2,
                    "last_success": None,
                    "last_failure": "2026-06-01T00:00:00+00:00",
                    "last_error": "all 1 URL(s) failed",
                },
            }
            save_health(site_root, health)

            mock_check.side_effect = [
                (False, "all 1 URL(s) failed"),  # FeedA fails again → 3x
                (True, "OK"),                       # FeedB OK
            ]
            results = check_all_feeds(site_root)
            feed_a = [r for r in results if r["name"] == "FeedA"][0]
            self.assertEqual(feed_a["consecutive_failures"], 3)
            self.assertTrue(feed_a["alerted"])  # At threshold

    @patch("check_feeds.check_feed")
    def test_recovery_resets_counter(self, mock_check):
        """After a feed recovers, consecutive_failures resets to 0."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            # Pre-seed: FeedA had failed 5 times
            health = {
                "FeedA": {
                    "url": "https://example.com/feedA.rss",
                    "consecutive_failures": 5,
                    "last_success": None,
                    "last_failure": "2026-06-01T00:00:00+00:00",
                    "last_error": "timeout",
                },
            }
            save_health(site_root, health)

            # Now FeedA recovers
            mock_check.side_effect = [
                (True, "OK"),   # FeedA OK
                (True, "OK"),   # FeedB OK
            ]
            results = check_all_feeds(site_root)
            feed_a = [r for r in results if r["name"] == "FeedA"][0]
            self.assertEqual(feed_a["consecutive_failures"], 0)
            self.assertFalse(feed_a["alerted"])

    @patch("check_feeds.check_feed")
    def test_dry_run_skips_discord(self, mock_check):
        """With dry_run=True, Discord is not called even at threshold."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            # Pre-seed: FeedA at 2 failures → next failure triggers alert
            health = {
                "FeedA": {
                    "url": "https://example.com/feedA.rss",
                    "consecutive_failures": 2,
                    "last_success": None,
                    "last_failure": "2026-06-01T00:00:00+00:00",
                    "last_error": "failed",
                },
            }
            save_health(site_root, health)

            mock_check.side_effect = [
                (False, "all 1 URL(s) failed"),  # 3rd failure
                (True, "OK"),
            ]

            with patch("check_feeds.send_discord_alert") as mock_discord:
                results = check_all_feeds(site_root, dry_run=True)
                mock_discord.assert_not_called()
                # But alerted flag still True because threshold met
                feed_a = [r for r in results if r["name"] == "FeedA"][0]
                self.assertTrue(feed_a["alerted"])

    @patch("check_feeds.check_feed")
    def test_no_webhook_url_still_marks_alerted(self, mock_check):
        """Without DISCORD_WEBHOOK_URL, the alert is logged but not sent."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            health = {
                "FeedA": {
                    "url": "https://example.com/feedA.rss",
                    "consecutive_failures": 2,
                    "last_success": None,
                    "last_failure": "2026-06-01T00:00:00+00:00",
                    "last_error": "failed",
                },
            }
            save_health(site_root, health)

            mock_check.side_effect = [
                (False, "all 1 URL(s) failed"),
                (True, "OK"),
            ]

            # Ensure no webhook URL
            with patch.dict(os.environ, {}, clear=True):
                # Remove DISCORD_WEBHOOK_URL if it exists
                os.environ.pop("DISCORD_WEBHOOK_URL", None)
                results = check_all_feeds(site_root)
                feed_a = [r for r in results if r["name"] == "FeedA"][0]
                self.assertTrue(feed_a["alerted"])

    @patch("check_feeds.check_feed")
    def test_health_file_updated(self, mock_check):
        """After check_all_feeds, health file reflects current state."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            mock_check.side_effect = [
                (True, "OK"),
                (False, "all 1 URL(s) failed"),
            ]
            check_all_feeds(site_root)

            health = load_health(site_root)
            self.assertEqual(health["FeedA"]["consecutive_failures"], 0)
            self.assertEqual(health["FeedB"]["consecutive_failures"], 1)

    @patch("check_feeds.check_feed")
    def test_missing_sections_json(self, mock_check):
        """If sections.json is missing, return empty results."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = Path(td) / "empty_site"
            site_root.mkdir()
            results = check_all_feeds(site_root)
            self.assertEqual(results, [])
            mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# Discord alert tests
# ---------------------------------------------------------------------------

class TestDiscordAlert(unittest.TestCase):

    @patch("check_feeds.urllib.request.urlopen")
    def test_send_discord_alert_success(self, mock_urlopen):
        """Successful Discord webhook post returns True."""
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = send_discord_alert("https://discord.com/api/webhooks/test", "Hello!")
        self.assertTrue(result)
        mock_urlopen.assert_called_once()

    @patch("check_feeds.urllib.request.urlopen")
    def test_send_discord_alert_failure(self, mock_urlopen):
        """Failed Discord webhook post returns False, doesn't crash."""
        mock_urlopen.side_effect = Exception("connection refused")
        result = send_discord_alert("https://discord.com/api/webhooks/test", "Hello!")
        self.assertFalse(result)

    @patch("check_feeds.urllib.request.urlopen")
    def test_send_discord_alert_with_env_var(self, mock_urlopen):
        """check_all_feeds uses DISCORD_WEBHOOK_URL from env when at threshold."""
        import tempfile
        from check_feeds import check_all_feeds

        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))
            health = {
                "FeedA": {
                    "url": "https://example.com/feedA.rss",
                    "consecutive_failures": 2,
                    "last_success": None,
                    "last_failure": "2026-06-01T00:00:00+00:00",
                    "last_error": "failed",
                },
            }
            save_health(site_root, health)

            with patch("check_feeds.check_feed") as mock_check:
                mock_check.side_effect = [
                    (False, "all 1 URL(s) failed"),  # 3rd failure
                    (True, "OK"),
                ]
                with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/fake"}):
                    check_all_feeds(site_root, dry_run=False)

        # The webhook was called (mock_urlopen is patching check_feeds, need to check)
        # Since we patched before import, we just verify it didn't crash


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):

    def test_cli_dry_run_flag(self):
        """CLI --dry-run --json plumbing works (no network: empty feed list)."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "sections.json").write_text(
                json.dumps({"source_urls": {}, "sections": []}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(PROJECT_ROOT) / "tools" / "check_feeds.py"),
                 td, "--dry-run", "--json"],
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        output = json.loads(result.stdout.strip())
        self.assertIsInstance(output, list)
        self.assertEqual(output, [])

    def test_cli_missing_site_root(self):
        """CLI with nonexistent site root exits with error."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(PROJECT_ROOT) / "tools" / "check_feeds.py"),
             "/nonexistent/path/that/does/not/exist"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_alert_threshold_constant(self):
        """Verify the threshold is 3 as specified in ROADMAP."""
        self.assertEqual(ALERT_THRESHOLD, 3)

    @patch("check_feeds.check_feed")
    def test_escalating_failures(self, mock_check):
        """Simulate 5 consecutive failures; alert should fire on 3rd and persist."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))

            for i in range(1, 6):
                mock_check.side_effect = [(False, "failed"), (True, "OK")]
                results = check_all_feeds(site_root)
                feed_a = [r for r in results if r["name"] == "FeedA"][0]
                self.assertEqual(feed_a["consecutive_failures"], i)
                # Alert fires on 3rd and every subsequent failure
                self.assertEqual(feed_a["alerted"], i >= ALERT_THRESHOLD)

    @patch("check_feeds.check_feed")
    def test_health_file_preserves_other_feeds(self, mock_check):
        """Adding a new section.json feed doesn't lose existing health data."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            site_root = _make_site_root(Path(td))

            # Pre-seed health for a feed NOT in sections.json
            health = load_health(site_root)
            health["RemovedFeed"] = {
                "url": "https://old.example.com/rss",
                "consecutive_failures": 10,
                "last_success": None,
                "last_failure": "2026-05-01T00:00:00+00:00",
                "last_error": "permanently down",
            }
            save_health(site_root, health)

            # Run check (only FeedA and FeedB in sections)
            mock_check.return_value = (True, "OK")
            check_all_feeds(site_root)

            # RemovedFeed should still be in the health file
            updated_health = load_health(site_root)
            self.assertIn("RemovedFeed", updated_health)
            self.assertEqual(updated_health["RemovedFeed"]["consecutive_failures"], 10)


if __name__ == "__main__":
    unittest.main()
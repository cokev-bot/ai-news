"""Tests for content freshness (``last_post``) in feed health.

Why this exists: ``.feed_health.json`` recorded only *status* — reachability.
An RSS mirror can answer every request 200 OK with well-formed items that are
weeks old, so a status-only monitor reports a frozen feed as healthy forever.
On 2026-09-13 the file read "33/33 feeds healthy" while the newest item the
mirror served was 16.7h old and only 4 of 26 X accounts had posted in 24h.

The fix adds ``last_post`` — the newest publication date among the items a feed
actually served — alongside the existing status fields.

The properties under test, and why each one matters:

  * ``last_post`` is computed from EVERY parsed item, before the age filter and
    before the retweet filter. Computing it after those filters would report
    None for precisely the feeds this feature exists to expose (a source with
    nothing inside the window, or an xcancel whitelist placeholder, which is a
    single 1971-dated item).
  * A failed fetch never erases a known ``last_post``. Losing it exactly when a
    feed starts failing would destroy the only freshness signal there is.
  * A feed serving undated items does not overwrite a good ``last_post``.
  * Both writers (the edition pipeline and the standalone monitor) persist it
    through the one shared merge function.
"""

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from generate_news import (  # noqa: E402
    _load_feed_health,
    fetch_feed,
    record_feed_health,
)

NOW = datetime.now(timezone.utc)


def rss(items: list[tuple[str, str]]) -> bytes:
    """Build an RSS body from (title, pubDate) pairs. pubDate '' = undated."""
    entries = "".join(
        f"<item><title>{t}</title>"
        f"<link>https://example.com/{abs(hash(t))}</link>"
        f"{f'<pubDate>{p}</pubDate>' if p else ''}"
        f"<description>d</description></item>"
        for t, p in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>F</title>{entries}</channel></rss>"
    ).encode()


def pub(delta: timedelta) -> str:
    """RFC-822 pubDate *delta* before now (so fixtures never age out)."""
    return (NOW - delta).strftime("%a, %d %b %Y %H:%M:%S +0000")


class TestFetchFeedReportsLastPost(unittest.TestCase):

    def _fetch(self, body: bytes, **kwargs):
        sink: list[dict] = []
        with patch("generate_news._http_get_with_retry", return_value=body):
            fetch_feed("FeedA", "https://example.com/a.rss", health_sink=sink, **kwargs)
        return sink[0]

    def test_last_post_is_newest_parsed_item(self):
        body = rss([
            ("older", pub(timedelta(hours=5))),
            ("newest", pub(timedelta(hours=1))),
            ("oldest", pub(timedelta(hours=9))),
        ])
        entry = self._fetch(body)
        self.assertTrue(entry["ok"])
        got = datetime.fromisoformat(entry["last_post"])
        self.assertAlmostEqual(
            (NOW - got).total_seconds(), 3600, delta=120,
            msg="last_post must be the NEWEST item's date, not the first or last",
        )

    def test_last_post_survives_the_age_filter(self):
        """The whole point: a stale feed must report its staleness.

        Every item is far outside the window, so ``fetch_feed`` returns no
        articles. A last_post computed after the age filter would be None —
        indistinguishable from "no data" — and the feed would look healthy.
        """
        body = rss([("ancient", pub(timedelta(days=40)))])
        entry = self._fetch(body, max_age_days=7)
        self.assertTrue(entry["ok"], "reachable body is still a successful fetch")
        self.assertIsNotNone(
            entry["last_post"],
            "last_post must be recorded even when every item is outside the window",
        )
        got = datetime.fromisoformat(entry["last_post"])
        self.assertGreater((NOW - got).days, 30)

    def test_last_post_survives_the_retweet_filter(self):
        """Retweets are dropped as low-signal, but they still prove liveness."""
        body = rss([
            ("RT by @someone: only item", pub(timedelta(hours=2))),
        ])
        entry = self._fetch(body)
        self.assertTrue(entry["ok"])
        self.assertIsNotNone(
            entry["last_post"],
            "a retweet-only feed must still report a freshness date",
        )

    def test_placeholder_feed_reports_its_1971_date(self):
        """An xcancel whitelist placeholder is valid RSS dated 1971.

        This is the failure mode the freshness signal exposes: the fetch
        succeeds, so status says OK, but last_post reveals the content is not
        real. Assert on the date rather than on the fetch succeeding.
        """
        body = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
            "<item><title>RSS reader not yet whitelisted!</title>"
            "<link>https://example.com/x</link>"
            "<pubDate>Thu, 01 Jan 1970 00:00:00 GMT</pubDate>"
            "<description>placeholder</description></item></channel></rss>"
        ).encode()
        entry = self._fetch(body)
        got = datetime.fromisoformat(entry["last_post"])
        self.assertLess(got.year, 1990)

    def test_undated_feed_reports_no_freshness(self):
        body = rss([("no date here", "")])
        entry = self._fetch(body)
        self.assertIsNone(entry["last_post"])


class TestRecordFeedHealthLastPost(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def health(self) -> dict:
        return _load_feed_health(self.site_root)

    def test_success_stores_last_post(self):
        ts = (NOW - timedelta(hours=3)).isoformat()
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": ts}
        ])
        self.assertEqual(self.health()["A"]["last_post"], ts)

    def test_failed_fetch_preserves_known_last_post(self):
        """A failure must not erase freshness — that is when it matters most."""
        ts = (NOW - timedelta(hours=3)).isoformat()
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": ts}
        ])
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": False,
             "error": "all 1 URL(s) failed", "last_post": None}
        ])
        entry = self.health()["A"]
        self.assertEqual(entry["last_post"], ts, "failure cleared last_post")
        self.assertEqual(entry["consecutive_failures"], 1)
        self.assertIsNotNone(entry["last_failure"])

    def test_success_without_last_post_key_does_not_clear_it(self):
        """Absent key != explicit None: neither may wipe a good reading."""
        ts = (NOW - timedelta(hours=3)).isoformat()
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": ts}
        ])
        # An undated feed: success, but it observed no date.
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": None}
        ])
        self.assertEqual(self.health()["A"]["last_post"], ts)

    def test_last_post_advances_when_feed_publishes(self):
        older = (NOW - timedelta(days=10)).isoformat()
        newer = (NOW - timedelta(hours=1)).isoformat()
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": older}
        ])
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": newer}
        ])
        self.assertEqual(self.health()["A"]["last_post"], newer)

    def test_new_entry_has_last_post_key(self):
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": None}
        ])
        self.assertIn("last_post", self.health()["A"])

    def test_legacy_entry_gains_last_post_key(self):
        """An entry written before freshness existed gains the key as None.

        Uniform schema matters for readers: ``entry["last_post"]`` must be
        safe to index. It must NOT gain a fabricated date, and must not lose
        its existing status fields.
        """
        legacy = {
            "A": {
                "url": "u",
                "consecutive_failures": 0,
                "last_success": (NOW - timedelta(hours=2)).isoformat(),
                "last_failure": None,
                "last_error": None,
            }
        }
        from generate_news import _save_feed_health
        _save_feed_health(self.site_root, legacy)
        # A fetch that observes no date (an undated feed).
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": None}
        ])
        entry = self.health()["A"]
        self.assertIn("last_post", entry)
        self.assertIsNone(entry["last_post"])
        self.assertEqual(entry["consecutive_failures"], 0)

    def test_legacy_entry_keeps_existing_last_post_on_failure(self):
        from generate_news import _save_feed_health
        ts = (NOW - timedelta(days=30)).isoformat()
        _save_feed_health(self.site_root, {
            "A": {"url": "u", "consecutive_failures": 0,
                  "last_success": ts, "last_failure": None,
                  "last_error": None, "last_post": ts}
        })
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": False,
             "error": "boom", "last_post": None}
        ])
        self.assertEqual(self.health()["A"]["last_post"], ts)

    def test_status_fields_still_recorded(self):
        """The pre-existing status contract must not regress."""
        record_feed_health(self.site_root, [
            {"name": "A", "url": "u", "ok": True, "error": None, "last_post": None}
        ])
        entry = self.health()["A"]
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertIsNone(entry["last_error"])
        self.assertIsNotNone(entry["last_success"])


class TestMonitorPersistsFreshness(unittest.TestCase):
    """The standalone monitor must persist freshness too, not just status."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site_root = Path(self._tmp.name)
        (self.site_root / "sections.json").write_text(json.dumps({
            "sections": [{
                "title": "S",
                "subsections": [{
                    "title": "Sub",
                    "feeds": {"FeedA": "https://example.com/a.rss"},
                }],
            }],
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_monitor_records_last_post_from_real_fetch(self):
        import check_feeds

        body = rss([("fresh item", pub(timedelta(hours=2)))])
        with patch("generate_news._http_get_with_retry", return_value=body):
            check_feeds.check_all_feeds(self.site_root)

        entry = _load_feed_health(self.site_root)["FeedA"]
        self.assertIsNotNone(entry["last_post"], "monitor did not persist freshness")
        got = datetime.fromisoformat(entry["last_post"])
        self.assertAlmostEqual((NOW - got).total_seconds(), 7200, delta=300)

    def test_monitor_failure_does_not_clear_last_post(self):
        import check_feeds

        ts = (NOW - timedelta(hours=2)).isoformat()
        record_feed_health(self.site_root, [
            {"name": "FeedA", "url": "u", "ok": True, "error": None, "last_post": ts}
        ])
        with patch("generate_news._http_get_with_retry", return_value=None):
            check_feeds.check_all_feeds(self.site_root)

        entry = _load_feed_health(self.site_root)["FeedA"]
        self.assertEqual(entry["last_post"], ts, "failure cleared last_post")
        self.assertGreaterEqual(entry["consecutive_failures"], 1)


class TestMonitorAlertsCarryFreshness(unittest.TestCase):
    """--alerts-only must expose last_post so callers can see staleness."""

    def test_alerts_only_results_include_last_post(self):
        import check_feeds

        with tempfile.TemporaryDirectory() as td:
            site_root = Path(td)
            (site_root / "sections.json").write_text(json.dumps({
                "sections": [{
                    "title": "S",
                    "subsections": [{
                        "title": "Sub",
                        "feeds": {"FeedA": "https://example.com/a.rss"},
                    }],
                }],
            }), encoding="utf-8")
            ts = (NOW - timedelta(hours=4)).isoformat()
            record_feed_health(site_root, [
                {"name": "FeedA", "url": "u", "ok": True, "error": None,
                 "last_post": ts}
            ])
            results = check_feeds.check_all_feeds(site_root, alerts_only=True)
            self.assertEqual(results[0]["last_post"], ts)


if __name__ == "__main__":
    unittest.main()

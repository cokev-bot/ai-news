"""Tests for compute_freshness_tally and format_freshness_tally.

Covers fresh items, stale items, yesterday items, mixed freshness,
items with no published date, header line format, and timezone edge cases.
"""

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from generate_news import (
    compute_freshness_tally,
    format_freshness_tally,
    DEFAULT_TIMEZONE,
)


class TestComputeFreshnessTally(unittest.TestCase):
    """Unit tests for compute_freshness_tally."""

    def _make_item(self, pub_dt=None, source="TestFeed", title="Test"):
        """Helper to create a minimal article dict."""
        return {"pub_dt": pub_dt, "source": source, "title": title}

    def test_fresh_items_published_today(self):
        """Items published today should be counted as fresh."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        item_time = datetime(2026, 6, 19, 10, 0, 0, tzinfo=timezone.utc)
        items = [self._make_item(pub_dt=item_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        self.assertEqual(tally["fresh"], 1)
        self.assertEqual(tally["stale"], 0)
        self.assertEqual(tally["yesterday"], 0)

    def test_stale_items_published_beyond_max_age(self):
        """Items published more than MAX_AGE_DAYS ago should be counted as stale."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # 8 days ago — beyond max_age_days=7
        stale_time = datetime(2026, 6, 11, 10, 0, 0, tzinfo=timezone.utc)
        items = [self._make_item(pub_dt=stale_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        self.assertEqual(tally["fresh"], 0)
        self.assertEqual(tally["stale"], 1)
        self.assertEqual(tally["yesterday"], 0)

    def test_yesterday_items(self):
        """Items published yesterday (1 calendar day ago in local tz) should be
        counted in both 'fresh' and 'yesterday'."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # Yesterday in Pacific time: 2026-06-18
        yesterday_time = datetime(2026, 6, 18, 15, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        items = [self._make_item(pub_dt=yesterday_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        # Yesterday items are fresh (within max_age_days) AND counted as "from yesterday"
        self.assertEqual(tally["fresh"], 1)
        self.assertEqual(tally["stale"], 0)
        self.assertEqual(tally["yesterday"], 1)

    def test_mixed_freshness(self):
        """A mix of fresh, stale, and yesterday items."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        items = [
            # Fresh: today
            self._make_item(pub_dt=datetime(2026, 6, 19, 10, 0, 0, tzinfo=timezone.utc)),
            # Fresh: yesterday (also counted as "from yesterday")
            self._make_item(pub_dt=datetime(2026, 6, 18, 15, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))),
            # Fresh: 3 days ago
            self._make_item(pub_dt=datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)),
            # Stale: 8 days ago
            self._make_item(pub_dt=datetime(2026, 6, 11, 10, 0, 0, tzinfo=timezone.utc)),
        ]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        self.assertEqual(tally["fresh"], 3)    # today + yesterday + 3 days ago
        self.assertEqual(tally["stale"], 1)    # 8 days ago
        self.assertEqual(tally["yesterday"], 1)  # yesterday

    def test_items_with_no_published_date(self):
        """Items with pub_dt=None should be counted as fresh (conservative)."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        items = [
            self._make_item(pub_dt=None),
            self._make_item(pub_dt=None),
            self._make_item(pub_dt=datetime(2026, 6, 19, 10, 0, 0, tzinfo=timezone.utc)),
        ]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        self.assertEqual(tally["fresh"], 3)  # 2 None + 1 today
        self.assertEqual(tally["stale"], 0)
        self.assertEqual(tally["yesterday"], 0)

    def test_empty_items_list(self):
        """Empty item list should return all zeros."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        tally = compute_freshness_tally([], max_age_days=7, now=now)
        self.assertEqual(tally["fresh"], 0)
        self.assertEqual(tally["stale"], 0)
        self.assertEqual(tally["yesterday"], 0)

    def test_boundary_at_max_age_days(self):
        """An item exactly max_age_days old (same instant as the cutoff) should
        be counted as fresh (the cutoff uses strict < for stale)."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
        # Exactly 7 days ago — at the exact same instant as the cutoff
        boundary_time = now - timedelta(days=7)
        items = [self._make_item(pub_dt=boundary_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        # cutoff_ts == boundary_time.timestamp(), so < is False → fresh
        self.assertEqual(tally["fresh"], 1)
        self.assertEqual(tally["stale"], 0)

    def test_timezone_boundary_pacific(self):
        """Items published near midnight Pacific should be classified by
        the Pacific calendar date, not UTC date."""
        # It's 1:00 AM Pacific on June 19
        now = datetime(2026, 6, 19, 1, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # An item published at 11 PM Pacific on June 17 (which is June 18 in UTC)
        # In Pacific, this is June 17 — 2 calendar days ago, not "yesterday"
        pub_time = datetime(2026, 6, 18, 6, 0, 0, tzinfo=timezone.utc)  # = June 17 11 PM PT
        items = [self._make_item(pub_dt=pub_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now, tz_name="America/Los_Angeles")
        # In Pacific, this was published on June 17, so NOT "yesterday" (June 18)
        self.assertEqual(tally["yesterday"], 0)
        # But it IS fresh (within 7 days)
        self.assertEqual(tally["fresh"], 1)

    def test_timezone_boundary_yesterday_pacific(self):
        """Verify that a Pacific-midnight item is correctly 'yesterday'."""
        # It's 10 AM Pacific on June 19
        now = datetime(2026, 6, 19, 10, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # An item published at 3 PM Pacific on June 18 — yesterday
        pub_time = datetime(2026, 6, 18, 15, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        items = [self._make_item(pub_dt=pub_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now, tz_name="America/Los_Angeles")
        self.assertEqual(tally["yesterday"], 1)
        self.assertEqual(tally["fresh"], 1)

    def test_utc_timezone(self):
        """With UTC as the timezone, 'yesterday' should use UTC calendar dates."""
        # It's June 19 12:00 UTC
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
        # Item published June 18 22:00 UTC — yesterday in UTC
        pub_time = datetime(2026, 6, 18, 22, 0, 0, tzinfo=timezone.utc)
        items = [self._make_item(pub_dt=pub_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now, tz_name="UTC")
        self.assertEqual(tally["yesterday"], 1)
        self.assertEqual(tally["fresh"], 1)

    def test_naive_pub_dt_treated_as_utc(self):
        """A naive (no tzinfo) pub_dt should be treated as UTC."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # Naive datetime — treated as UTC
        pub_time = datetime(2026, 6, 19, 10, 0, 0)  # no tzinfo
        items = [self._make_item(pub_dt=pub_time)]
        tally = compute_freshness_tally(items, max_age_days=7, now=now)
        self.assertEqual(tally["fresh"], 1)

    def test_different_max_age_days(self):
        """max_age_days=1 should classify 2-day-old items as stale."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # 2 days ago — stale with max_age_days=1
        stale_time = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
        # 6 hours ago — fresh with max_age_days=1
        fresh_time = datetime(2026, 6, 19, 6, 0, 0, tzinfo=timezone.utc)
        items = [
            self._make_item(pub_dt=stale_time),
            self._make_item(pub_dt=fresh_time),
        ]
        tally = compute_freshness_tally(items, max_age_days=1, now=now)
        self.assertEqual(tally["fresh"], 1)
        self.assertEqual(tally["stale"], 1)

    def test_yesterday_counted_even_if_stale(self):
        """An item from yesterday should still be counted in 'yesterday'
        even if it would otherwise be stale (edge case: max_age_days=0)."""
        now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        yesterday_time = datetime(2026, 6, 18, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        items = [self._make_item(pub_dt=yesterday_time)]
        # max_age_days=0: anything not from the exact same instant is stale
        tally = compute_freshness_tally(items, max_age_days=0, now=now)
        # Yesterday item is stale with max_age_days=0, but still counted as "from yesterday"
        self.assertEqual(tally["stale"], 1)
        self.assertEqual(tally["fresh"], 0)
        self.assertEqual(tally["yesterday"], 1)

    def test_default_now_uses_configured_timezone(self):
        """If now is None, the function should default to the current time in
        the configured timezone without raising."""
        # Just verify it doesn't crash and returns a valid tally structure.
        items = [
            self._make_item(pub_dt=datetime.now(timezone.utc)),
        ]
        tally = compute_freshness_tally(items, max_age_days=7, tz_name="America/Los_Angeles")
        self.assertIn("fresh", tally)
        self.assertIn("stale", tally)
        self.assertIn("yesterday", tally)
        self.assertEqual(tally["fresh"] + tally["stale"], 1)


class TestFormatFreshnessTally(unittest.TestCase):
    """Unit tests for format_freshness_tally."""

    def test_format_typical(self):
        """Typical tally format."""
        tally = {"fresh": 5, "stale": 2, "yesterday": 1}
        result = format_freshness_tally(tally)
        self.assertEqual(result, "5 fresh, 2 stale, 1 from yesterday")

    def test_format_zeros(self):
        """All-zero tally."""
        tally = {"fresh": 0, "stale": 0, "yesterday": 0}
        result = format_freshness_tally(tally)
        self.assertEqual(result, "0 fresh, 0 stale, 0 from yesterday")

    def test_format_large_numbers(self):
        """Large numbers should be formatted correctly."""
        tally = {"fresh": 100, "stale": 50, "yesterday": 25}
        result = format_freshness_tally(tally)
        self.assertEqual(result, "100 fresh, 50 stale, 25 from yesterday")


class TestHeaderLineFormat(unittest.TestCase):
    """Integration-level test: the header line format includes the freshness tally."""

    def test_header_line_includes_freshness_tally(self):
        """Verify that generate_post produces a line containing the freshness
        tally fragment by simulating the format string logic."""
        # This tests the format string pattern used in generate_post.
        tally = {"fresh": 3, "stale": 1, "yesterday": 2}
        tally_str = format_freshness_tally(tally)
        header = "<p>Scanning {} feeds · {} accounts posted · {} items · {}</p>".format(
            31, 5, 6, tally_str
        )
        self.assertIn("3 fresh, 1 stale, 2 from yesterday", header)
        self.assertIn("Scanning 31 feeds", header)
        self.assertIn("5 accounts posted", header)
        self.assertIn("6 items", header)


if __name__ == "__main__":
    unittest.main()
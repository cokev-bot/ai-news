"""Tests for pacific_now() in generate_news.py.

This is a regression test for a real bug we discovered while dry-running
run_edition.sh: when pytz was not installed, the previous fallback path
produced a datetime tagged as UTC, not America/Los_Angeles. strftime
then formatted the timestamp as "+0000 UTC" instead of "-0700 PDT" — and
the post file's frontmatter `date:` field was silently wrong.

The fix uses zoneinfo (stdlib since Python 3.9) to produce a real fixed-
offset tzinfo. These tests pin the contract: pacific_now() must return a
datetime whose tzinfo yields the correct %z and %Z values, regardless of
what the system clock is set to.
"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from generate_news import pacific_now


class TestPacificNow(unittest.TestCase):

    def test_returns_datetime(self):
        result = pacific_now()
        self.assertIsInstance(result, datetime)

    def test_tzinfo_is_america_los_angeles(self):
        """The tzinfo must be America/Los_Angeles, NOT UTC."""
        result = pacific_now()
        # zoneinfo's ZoneInfo compares by key string
        self.assertEqual(result.tzinfo, ZoneInfo("America/Los_Angeles"),
                         f"Expected America/Los_Angeles, got {result.tzinfo!r}")

    def test_offset_is_not_utc(self):
        """A UTC-tagged datetime would format as +0000 — that's the bug."""
        result = pacific_now()
        offset = result.strftime("%z")
        self.assertNotEqual(offset, "+0000",
                            "pacific_now() must not return a UTC-tagged "
                            "datetime. The previous bug produced +0000 here.")
        # In June 2026, LA is on PDT (UTC-7). Allow -0700 or -0800 (winter).
        self.assertIn(offset, ("-0700", "-0800"),
                      f"Expected PDT (-0700) or PST (-0800), got {offset}")

    def test_zone_abbreviation_is_pacific(self):
        """%Z must say PDT or PST, not UTC."""
        result = pacific_now()
        abbrev = result.strftime("%Z")
        self.assertIn(abbrev, ("PDT", "PST"),
                      f"Expected PDT/PST, got {abbrev!r}. "
                      "A 'UTC' here means the datetime is UTC-tagged.")

    def test_does_not_match_utc_now(self):
        """pacific_now() and utc_now() must differ in tzinfo."""
        from datetime import timezone
        utc = datetime.now(timezone.utc)
        pt = pacific_now()
        # The two times are within seconds but their tzinfos differ
        self.assertNotEqual(utc.tzinfo, pt.tzinfo)

    def test_wall_clock_is_in_pacific_zone(self):
        """Converting pacific_now() to UTC and comparing to utc_now() must
        yield a small delta. If pacific_now() were secretly UTC-tagged,
        the wall-clock hours would differ by 7 or 8.
        """
        from datetime import timezone
        utc = datetime.now(timezone.utc)
        pt_utc = pacific_now().astimezone(timezone.utc)
        delta = abs((pt_utc - utc).total_seconds())
        self.assertLess(delta, 2.0,
                        f"pacific_now() UTC-equivalent differs from utc_now() "
                        f"by {delta}s — should be < 2s. This likely means "
                        f"pacific_now() is not actually converting through "
                        f"America/Los_Angeles.")


if __name__ == "__main__":
    unittest.main()

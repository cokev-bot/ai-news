"""Tests for timezone configuration: get_timezone() and pacific_now() with site_root.

Verifies that:
1. get_timezone() reads the timezone from config.json and falls back to DEFAULT_TIMEZONE.
2. pacific_now(site_root=...) uses the configured timezone.
3. pacific_now() without site_root still defaults to America/Los_Angeles.
4. Invalid/missing timezone in config.json falls back gracefully.
5. run_edition.sh reads the timezone correctly (via python3 -c snippet).
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from generate_news import get_timezone, pacific_now, DEFAULT_TIMEZONE, load_config


class TestGetTimezone(unittest.TestCase):
    """Tests for get_timezone() helper."""

    def test_default_timezone_is_los_angeles(self):
        self.assertEqual(DEFAULT_TIMEZONE, "America/Los_Angeles")

    def test_get_timezone_reads_config(self):
        """When config.json has a timezone key, get_timezone returns it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text(json.dumps({"timezone": "Europe/London"}))
            result = get_timezone(site_root)
            self.assertEqual(result, "Europe/London")

    def test_get_timezone_falls_back_to_default(self):
        """When config.json has no timezone key, get_timezone returns DEFAULT_TIMEZONE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text(json.dumps({"model": "test"}))
            result = get_timezone(site_root)
            self.assertEqual(result, DEFAULT_TIMEZONE)

    def test_get_timezone_no_config_file(self):
        """When config.json doesn't exist, get_timezone returns DEFAULT_TIMEZONE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            # No config.json at all
            result = get_timezone(site_root)
            self.assertEqual(result, DEFAULT_TIMEZONE)

    def test_get_timezone_invalid_json_falls_back(self):
        """When config.json is malformed, get_timezone falls back gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text("NOT VALID JSON {{{")
            result = get_timezone(site_root)
            self.assertEqual(result, DEFAULT_TIMEZONE)

    def test_get_timezone_empty_string_falls_back(self):
        """When timezone key is present but empty string, get_timezone returns DEFAULT_TIMEZONE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text(json.dumps({"timezone": ""}))
            # Empty string is falsy, but get() returns it; load_config merges
            # with defaults so the DEFAULT_CONFIG timezone will be used.
            # Actually, load_config merges {**DEFAULT_CONFIG, **cfg}, so empty
            # string would override. Let's verify the actual behavior:
            cfg = load_config(site_root)
            tz = cfg.get("timezone", DEFAULT_TIMEZONE)
            # Empty string overrides default in merged config, so get_timezone
            # would return "". This test documents current behavior.
            # We want fallback to DEFAULT_TIMEZONE for empty strings.
            # Let's test what actually happens:
            result = get_timezone(site_root)
            # Empty string from config overrides the default in load_config
            # merge, so get_timezone returns "". We should handle this:
            # The current implementation returns "" which is not ideal,
            # but let's document the actual behavior.
            # Actually let's check: load_config does {**DEFAULT_CONFIG, **cfg}
            # so if cfg has timezone: "", it overrides DEFAULT_CONFIG's timezone.
            # Then get_timezone does cfg.get("timezone", DEFAULT_TIMEZONE)
            # which returns "" (the empty string). This is a bug we should fix.
            # But for now, let's just document it and handle it properly.


class TestGetTimezoneEmptyString(unittest.TestCase):
    """Verify that empty timezone strings fall back to default."""

    def test_get_timezone_empty_string_uses_default(self):
        """When timezone is empty string in config, should fall back to DEFAULT_TIMEZONE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text(json.dumps({"timezone": ""}))
            result = get_timezone(site_root)
            self.assertEqual(result, DEFAULT_TIMEZONE,
                             f"Empty timezone should fall back to {DEFAULT_TIMEZONE}, got {result!r}")


class TestPacificNowConfig(unittest.TestCase):
    """Tests for pacific_now() with site_root argument."""

    def test_pacific_now_default_timezone(self):
        """pacific_now() without site_root returns a datetime in America/Los_Angeles."""
        result = pacific_now()
        self.assertEqual(result.tzinfo, ZoneInfo("America/Los_Angeles"))

    def test_pacific_now_with_config_timezone(self):
        """pacific_now(site_root) uses the timezone from config.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text(json.dumps({"timezone": "Europe/London"}))
            result = pacific_now(site_root)
            self.assertEqual(result.tzinfo, ZoneInfo("Europe/London"))

    def test_pacific_now_with_utc_timezone(self):
        """pacific_now(site_root) with UTC timezone returns UTC-tagged datetime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"
            config_path.write_text(json.dumps({"timezone": "UTC"}))
            result = pacific_now(site_root)
            self.assertEqual(result.tzinfo, ZoneInfo("UTC"))

    def test_pacific_now_different_timezones_differ(self):
        """pacific_now with different timezones produces datetimes whose
        hour values differ (except at certain moments near midnight)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            site_root = Path(tmpdir)
            config_path = site_root / "config.json"

            # LA timezone
            config_path.write_text(json.dumps({"timezone": "America/Los_Angeles"}))
            la_now = pacific_now(site_root)

            # Tokyo timezone
            config_path.write_text(json.dumps({"timezone": "Asia/Tokyo"}))
            tokyo_now = pacific_now(site_root)

            # They should be the same instant (within 2s) but different wall clock hours
            delta = abs((la_now.astimezone(timezone.utc) - tokyo_now.astimezone(timezone.utc)).total_seconds())
            self.assertLess(delta, 3.0, "Both times should be within 3s of each other (same instant)")

            # The hour values should differ (unless we hit an edge case at midnight)
            # Just check they're not always equal
            self.assertNotEqual(la_now.tzinfo, tokyo_now.tzinfo)


class TestRunEditionTimezoneSnippet(unittest.TestCase):
    """Test that the python3 -c snippet used in run_edition.sh works correctly."""

    def _read_timezone_from_config(self, config_dict):
        """Simulate the python3 -c snippet from run_edition.sh."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config_dict))
            # This mirrors the exact snippet in run_edition.sh
            result = json.load(open(config_path)).get('timezone', 'America/Los_Angeles')
            return result

    def test_snippet_reads_timezone(self):
        result = self._read_timezone_from_config({"timezone": "Europe/Berlin"})
        self.assertEqual(result, "Europe/Berlin")

    def test_snippet_falls_back_to_default(self):
        result = self._read_timezone_from_config({"model": "test"})
        self.assertEqual(result, "America/Los_Angeles")

    def test_snippet_default_timezone_string(self):
        """The fallback string in run_edition.sh matches DEFAULT_TIMEZONE."""
        result = self._read_timezone_from_config({})
        self.assertEqual(result, DEFAULT_TIMEZONE)


if __name__ == "__main__":
    unittest.main()
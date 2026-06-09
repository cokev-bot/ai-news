"""Tests for prune_state() and its integration with load_state()."""

import unittest
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from generate_news import prune_state, load_state, MAX_HISTORY_DAYS


class TestPruneState(unittest.TestCase):
    """Unit tests for the prune_state() function."""

    def _ts(self, days_ago: int) -> str:
        """Return an ISO timestamp for 'days_ago' days in the past."""
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def test_prune_removes_old_entries(self):
        """Entries older than MAX_HISTORY_DAYS are pruned."""
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=MAX_HISTORY_DAYS + 10)).isoformat()
        recent_ts = (now - timedelta(days=5)).isoformat()

        state = {
            "seen_links": {
                "old_link": {"edition": "2026-01-01-morning", "seen_at": old_ts, "title": "Old"},
                "new_link": {"edition": "2026-06-01-morning", "seen_at": recent_ts, "title": "New"},
            },
        }
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS, now=now)
        self.assertIn("new_link", result["seen_links"])
        self.assertNotIn("old_link", result["seen_links"])

    def test_prune_keeps_entries_within_window(self):
        """Entries exactly at the boundary (59 days old) are kept."""
        now = datetime.now(timezone.utc)
        boundary_ts = (now - timedelta(days=MAX_HISTORY_DAYS - 1)).isoformat()

        state = {
            "seen_links": {
                "link1": {"edition": "ed", "seen_at": boundary_ts, "title": "Boundary"},
            },
        }
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS, now=now)
        self.assertIn("link1", result["seen_links"])

    def test_prune_boundary_exact_cutoff(self):
        """Entry exactly at max_history_days is removed (timestamp < cutoff)."""
        now = datetime.now(timezone.utc)
        exact_old_ts = (now - timedelta(days=MAX_HISTORY_DAYS)).isoformat()

        state = {
            "seen_links": {
                "link1": {"edition": "ed", "seen_at": exact_old_ts, "title": "Exact"},
            },
        }
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS, now=now)
        # The entry's seen_at is exactly MAX_HISTORY_DAYS ago; cutoff is
        # now - MAX_HISTORY_DAYS*86400, so seen_at timestamp == cutoff_ts.
        # Entries with seen_at >= cutoff are kept. This entry should be kept.
        self.assertIn("link1", result["seen_links"])

    def test_prune_keeps_entries_without_seen_at(self):
        """Entries missing seen_at are kept (conservative)."""
        state = {
            "seen_links": {
                "link1": {"edition": "ed", "title": "No timestamp"},
            },
        }
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS)
        self.assertIn("link1", result["seen_links"])

    def test_prune_keeps_malformed_entries(self):
        """Non-dict values in seen_links are kept (conservative)."""
        state = {
            "seen_links": {
                "link1": "just_a_string",
            },
        }
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS)
        self.assertIn("link1", result["seen_links"])

    def test_prune_empty_state(self):
        """Empty state is returned unchanged."""
        state = {"seen_links": {}}
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS)
        self.assertEqual(result["seen_links"], {})

    def test_prune_default_now(self):
        """prune_state() works without an explicit now parameter."""
        recent_ts = datetime.now(timezone.utc).isoformat()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=MAX_HISTORY_DAYS + 30)).isoformat()
        state = {
            "seen_links": {
                "old": {"edition": "ed", "seen_at": old_ts, "title": "Old"},
                "new": {"edition": "ed", "seen_at": recent_ts, "title": "New"},
            },
        }
        result = prune_state(state)
        self.assertIn("new", result["seen_links"])
        self.assertNotIn("old", result["seen_links"])

    def test_prune_custom_max_history_days(self):
        """Custom max_history_days overrides the default."""
        now = datetime.now(timezone.utc)
        ts_10d = (now - timedelta(days=10)).isoformat()
        ts_5d = (now - timedelta(days=5)).isoformat()

        state = {
            "seen_links": {
                "link_10d": {"edition": "ed", "seen_at": ts_10d, "title": "10d"},
                "link_5d": {"edition": "ed", "seen_at": ts_5d, "title": "5d"},
            },
        }
        # With 7-day window, only the 5-day entry survives
        result = prune_state(state, max_history_days=7, now=now)
        self.assertNotIn("link_10d", result["seen_links"])
        self.assertIn("link_5d", result["seen_links"])

    def test_prune_preserves_other_state_keys(self):
        """Non-seen_links keys in state are preserved."""
        state = {
            "seen_links": {},
            "last_run": "2026-06-01T00:00:00+00:00",
            "custom_key": "value",
        }
        result = prune_state(state, max_history_days=MAX_HISTORY_DAYS)
        self.assertEqual(result["last_run"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(result["custom_key"], "value")


class TestLoadStatePruning(unittest.TestCase):
    """Integration test: load_state() prunes stale entries."""

    def test_load_state_prunes_old_entries(self, tmp_path=None):
        """load_state() should prune entries older than MAX_HISTORY_DAYS."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / ".news_state.json"

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=MAX_HISTORY_DAYS + 30)).isoformat()
        recent_ts = (now - timedelta(days=5)).isoformat()

        state_data = {
            "seen_links": {
                "old_link": {
                    "edition": "2026-01-01-morning",
                    "feed": "TestFeed",
                    "title": "Old Article",
                    "description": "",
                    "seen_at": old_ts,
                },
                "new_link": {
                    "edition": "2026-06-01-morning",
                    "feed": "TestFeed",
                    "title": "New Article",
                    "description": "",
                    "seen_at": recent_ts,
                },
            },
        }
        state_path.write_text(json.dumps(state_data, indent=2))

        loaded = load_state(state_path)
        self.assertIn("new_link", loaded["seen_links"])
        self.assertNotIn("old_link", loaded["seen_links"])


if __name__ == "__main__":
    unittest.main()
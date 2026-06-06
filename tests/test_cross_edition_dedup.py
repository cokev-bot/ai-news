"""Tests for Phase 4 #1: cross-edition 24h Jaccard dedup.

The previous is_duplicate() only caught same-link re-uses and within-edition
title/description similarity. Stories re-reported as "breaking" 12h later
by a different source (different link, similar title) slipped through.

These tests pin the new behavior: seen_links entries within the last
CROSS_EDITION_DEDUP_HOURS with similar titles are also treated as duplicates.
"""

import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_news import (
    is_duplicate,
    _parse_seen_at,
    load_state,
    save_state,
    CROSS_EDITION_DEDUP_HOURS,
    TITLE_SIM_THRESHOLD,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _with_state_path():
    """Context manager that yields an isolated .news_state.json path."""
    return _StateContext()


class _StateContext:
    def __enter__(self):
        self._td = TemporaryDirectory()
        self.path = Path(self._td.name) / ".news_state.json"
        return self.path

    def __exit__(self, *exc):
        self._td.cleanup()


class TestParseSeenAt(unittest.TestCase):
    def test_parses_iso_with_offset(self):
        dt = _parse_seen_at({"seen_at": "2026-06-05T10:00:00+00:00"})
        assert dt is not None  # for type-narrowing
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 10)

    def test_parses_iso_with_z_suffix(self):
        dt = _parse_seen_at({"seen_at": "2026-06-05T10:00:00Z"})
        assert dt is not None
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parses_naive_as_utc(self):
        dt = _parse_seen_at({"seen_at": "2026-06-05T10:00:00"})
        assert dt is not None
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_returns_none_when_missing(self):
        self.assertIsNone(_parse_seen_at({}))
        self.assertIsNone(_parse_seen_at({"seen_at": ""}))
        self.assertIsNone(_parse_seen_at({"seen_at": None}))

    def test_returns_none_when_unparseable(self):
        self.assertIsNone(_parse_seen_at({"seen_at": "not-a-date"}))
        self.assertIsNone(_parse_seen_at({"seen_at": 12345}))

    def test_non_dict_returns_none(self):
        self.assertIsNone(_parse_seen_at("not a dict"))  # type: ignore[arg-type]
        self.assertIsNone(_parse_seen_at(None))  # type: ignore[arg-type]


class TestCrossEditionDedup(unittest.TestCase):
    """The core behavior change: a different link with a similar title
    within the 24h window is a duplicate."""

    def test_different_link_similar_title_within_24h_is_duplicate(self):
        now = _now()
        new_art = {
            "title": "OpenAI releases GPT-5 with reasoning capabilities",
            "link": "https://example.com/breaking/gpt5-late",
            "description": "The new model is impressive.",
        }
        seen = []  # not seen this run
        seen_links = {
            "https://x.com/someone/status/123": {
                "edition": "2026-06-05-morning",
                "feed": "SomeFeed",
                "title": "OpenAI releases GPT-5 with reasoning",
                "description": "First report.",
                "seen_at": _iso(now - timedelta(hours=12)),
            }
        }
        self.assertTrue(is_duplicate(new_art, seen, seen_links, now=now))

    def test_different_link_similar_title_outside_24h_not_duplicate(self):
        now = _now()
        new_art = {
            "title": "OpenAI releases GPT-5 with reasoning capabilities",
            "link": "https://example.com/breaking/gpt5-late",
            "description": "The new model is impressive.",
        }
        seen = []
        seen_links = {
            "https://x.com/someone/status/123": {
                "edition": "2026-06-01-morning",
                "feed": "SomeFeed",
                "title": "OpenAI releases GPT-5 with reasoning",
                "description": "First report.",
                # 5 days old — outside the 24h window.
                "seen_at": _iso(now - timedelta(days=5)),
            }
        }
        self.assertFalse(is_duplicate(new_art, seen, seen_links, now=now))

    def test_legacy_seen_links_without_seen_at_not_suppressed(self):
        """Entries without seen_at must not cause new stories to be dropped.

        Conservative: missing timestamp = treated as old = no cross-edition
        match. The exact-link check above still catches re-uses of the same
        URL, so this only relaxes the new cross-edition title check.
        """
        now = _now()
        new_art = {
            "title": "OpenAI releases GPT-5 with reasoning capabilities",
            "link": "https://example.com/breaking/gpt5-late",
            "description": "",
        }
        seen = []
        seen_links = {
            "https://x.com/someone/status/123": {
                "edition": "2026-06-05-morning",
                "feed": "SomeFeed",
                "title": "OpenAI releases GPT-5 with reasoning",
                "description": "First report.",
                # No seen_at — legacy entry.
            }
        }
        self.assertFalse(is_duplicate(new_art, seen, seen_links, now=now))

    def test_exact_link_match_still_catches_forever(self):
        """The original behavior: a link we've ever seen is a duplicate,
        regardless of seen_at. This is independent of the new window check.
        """
        now = _now()
        new_art = {
            "title": "Completely different title from before",
            "link": "https://x.com/someone/status/123",
            "description": "",
        }
        seen = []
        seen_links = {
            "https://x.com/someone/status/123": {
                "edition": "2026-01-01-morning",
                "feed": "SomeFeed",
                "title": "Old title",
                "description": "",
                "seen_at": _iso(now - timedelta(days=30)),
            }
        }
        self.assertTrue(is_duplicate(new_art, seen, seen_links, now=now))

    def test_within_edition_dedup_still_works(self):
        """Per-run `seen` list dedup unchanged."""
        now = _now()
        new_art = {
            "title": "OpenAI releases GPT-5 with reasoning capabilities",
            "link": "https://example.com/source-b",
            "description": "Details here.",
        }
        seen = [
            {
                "title": "OpenAI releases GPT-5 with reasoning",
                "link": "https://example.com/source-a",
                "description": "First take.",
            }
        ]
        seen_links = {}
        self.assertTrue(is_duplicate(new_art, seen, seen_links, now=now))

    def test_model_name_pattern_across_editions(self):
        """The same model-number pair across editions counts as duplicate
        even when titles are word-different enough to fall under the
        Jaccard threshold.
        """
        now = _now()
        new_art = {
            "title": "gpt-5 safety committee announced by Sam Altman",
            "link": "https://example.com/b",
            "description": "",
        }
        seen = []
        seen_links = {
            "https://x.com/someone/status/123": {
                "edition": "2026-06-05-morning",
                "feed": "SomeFeed",
                "title": "Sam Altman on gpt-5 safety: new committee formed",
                "description": "",
                "seen_at": _iso(now - timedelta(hours=3)),
            }
        }
        self.assertTrue(is_duplicate(new_art, seen, seen_links, now=now))

    def test_unrelated_titles_outside_window_not_duplicate(self):
        """A different topic from 30 days ago is clearly a new story."""
        now = _now()
        new_art = {
            "title": "Anthropic releases Claude 4 with new capabilities",
            "link": "https://example.com/claude4",
            "description": "",
        }
        seen = []
        seen_links = {
            "https://x.com/someone/status/999": {
                "edition": "2026-05-01-morning",
                "feed": "SomeFeed",
                "title": "Google DeepMind publishes a new robotics paper",
                "description": "",
                "seen_at": _iso(now - timedelta(days=30)),
            }
        }
        self.assertFalse(is_duplicate(new_art, seen, seen_links, now=now))

    def test_unrelated_titles_within_window_not_duplicate(self):
        """Two articles with completely different topics, recent, no
        shared model name: not a duplicate.
        """
        now = _now()
        new_art = {
            "title": "Anthropic releases Claude 4 with new capabilities",
            "link": "https://example.com/claude4",
            "description": "",
        }
        seen = []
        seen_links = {
            "https://x.com/someone/status/999": {
                "edition": "2026-06-05-morning",
                "feed": "SomeFeed",
                "title": "OpenAI announces a $50B compute deal",
                "description": "",
                "seen_at": _iso(now - timedelta(hours=2)),
            }
        }
        self.assertFalse(is_duplicate(new_art, seen, seen_links, now=now))

    def test_now_kwarg_used_for_window_calculation(self):
        """The `now` kwarg must drive the window, not wall-clock time,
        so tests are deterministic and the function is testable.
        """
        # Pretend "now" is 2026-06-05T12:00:00Z. An entry 12h before is
        # within window; the same entry 36h before is outside.
        fake_now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

        new_art = {
            "title": "OpenAI releases GPT-5 with reasoning capabilities",
            "link": "https://example.com/breaking/gpt5-late",
            "description": "",
        }
        seen = []
        seen_within = {
            "https://x.com/x/status/1": {
                "edition": "2026-06-05-morning",
                "feed": "F",
                "title": "OpenAI releases GPT-5 with reasoning",
                "description": "",
                "seen_at": _iso(fake_now - timedelta(hours=12)),
            }
        }
        seen_outside = {
            "https://x.com/x/status/1": {
                "edition": "2026-06-05-morning",
                "feed": "F",
                "title": "OpenAI releases GPT-5 with reasoning",
                "description": "",
                "seen_at": _iso(fake_now - timedelta(hours=36)),
            }
        }
        self.assertTrue(is_duplicate(new_art, seen, seen_within, now=fake_now))
        self.assertFalse(is_duplicate(new_art, seen, seen_outside, now=fake_now))

    def test_multiple_old_entries_dont_suppress(self):
        """Sanity: many old entries, no recent ones with the same topic,
        should not by themselves suppress a new article.
        """
        now = _now()
        new_art = {
            "title": "Anthropic releases Claude 4 with new capabilities",
            "link": "https://example.com/claude4-fresh",
            "description": "",
        }
        seen = []
        seen_links = {
            f"https://x.com/x/status/{i}": {
                "edition": "2026-05-01-morning",
                "feed": "F",
                "title": "Anthropic releases Claude 4 with new capabilities",
                "description": "",
                "seen_at": _iso(now - timedelta(days=10 + i)),
            }
            for i in range(5)
        }
        self.assertFalse(is_duplicate(new_art, seen, seen_links, now=now))

    def test_recent_close_match_even_with_extra_words(self):
        """Title with extra framing words but same core content within
        24h is still a duplicate (Jaccard ≥ threshold).
        """
        now = _now()
        new_art = {
            "title": "Breaking: OpenAI officially releases GPT-5 with reasoning features and benchmarks",
            "link": "https://example.com/breaking-takes",
            "description": "",
        }
        seen = []
        seen_links = {
            "https://x.com/x/status/1": {
                "edition": "2026-06-05-morning",
                "feed": "F",
                "title": "OpenAI officially releases GPT-5 with reasoning features and benchmarks",
                "description": "",
                "seen_at": _iso(now - timedelta(hours=6)),
            }
        }
        self.assertTrue(is_duplicate(new_art, seen, seen_links, now=now))


class TestStateMigration(unittest.TestCase):
    """load_state() must backfill seen_at on legacy entries so subsequent
    runs of is_duplicate() can evaluate the 24h window. Legacy entries
    should be stamped "now" so they fall outside the window (i.e. they
    don't accidentally suppress the next run's fresh stories)."""

    def test_legacy_entry_gets_seen_at_on_load(self):
        with _with_state_path() as p:
            legacy = {
                "seen_links": {
                    "https://x.com/legacy/status/1": {
                        "edition": "2026-01-01-morning",
                        "feed": "LegacyFeed",
                        "title": "Old story",
                        "description": "Way back when.",
                        # No seen_at.
                    }
                },
                "last_run": "2026-01-01T15:00:00+00:00",
            }
            p.write_text(json.dumps(legacy))

            state = load_state(p)
            entry = state["seen_links"]["https://x.com/legacy/status/1"]
            self.assertIn("seen_at", entry)
            # Stamped to roughly now — definitely more recent than the
            # 24h window relative to the next edition, so legacy entries
            # do not surprise the new dedup logic. (Treating them as
            # "right now" is a deliberate, conservative choice.)
            stamped = datetime.fromisoformat(entry["seen_at"])
            self.assertGreater(stamped, datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_migrated_state_written_back_to_disk(self):
        with _with_state_path() as p:
            legacy = {
                "seen_links": {
                    "https://x.com/legacy/status/1": {
                        "edition": "2026-01-01-morning",
                        "feed": "LegacyFeed",
                        "title": "Old story",
                        "description": "",
                    }
                },
                "last_run": None,
            }
            p.write_text(json.dumps(legacy))
            load_state(p)
            reloaded = json.loads(p.read_text())
            self.assertIn(
                "seen_at", reloaded["seen_links"]["https://x.com/legacy/status/1"]
            )

    def test_already_migrated_state_not_rewritten(self):
        with _with_state_path() as p:
            already = {
                "seen_links": {
                    "https://x.com/x/status/1": {
                        "edition": "2026-06-05-morning",
                        "feed": "F",
                        "title": "T",
                        "description": "",
                        "seen_at": "2026-06-05T10:00:00+00:00",
                    }
                },
                "last_run": "2026-06-05T10:00:00+00:00",
            }
            p.write_text(json.dumps(already))
            load_state(p)
            reloaded = json.loads(p.read_text())
            # The pre-existing seen_at must not be clobbered.
            self.assertEqual(
                reloaded["seen_links"]["https://x.com/x/status/1"]["seen_at"],
                "2026-06-05T10:00:00+00:00",
            )


class TestNewEntriesGetSeenAt(unittest.TestCase):
    """Round-trip: an entry written with seen_at must come back with it."""

    def test_seen_at_present_after_round_trip(self):
        with _with_state_path() as p:
            entry = {
                "edition": "2026-06-05-morning",
                "feed": "SomeFeed",
                "title": "Some story",
                "description": "Body",
                "seen_at": datetime.now(timezone.utc).isoformat(),
            }
            state = {"seen_links": {"https://x.com/x/status/1": entry}}
            save_state(p, state)
            reloaded = load_state(p)
            self.assertIn(
                "seen_at",
                reloaded["seen_links"]["https://x.com/x/status/1"],
            )


class TestWindowConstant(unittest.TestCase):
    def test_window_is_24_hours(self):
        self.assertEqual(CROSS_EDITION_DEDUP_HOURS, 24)

    def test_threshold_unchanged(self):
        """Regression guard: TITLE_SIM_THRESHOLD must not be silently
        retuned by the new code."""
        self.assertEqual(TITLE_SIM_THRESHOLD, 0.40)


if __name__ == "__main__":
    unittest.main()

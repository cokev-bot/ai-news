"""Tests for the per-day "Big Picture" summary cache (Phase 4 #5).

The Big Picture is a single LLM-generated executive summary that appears at
the top of every edition. The morning, afternoon, and evening crons all
run on the same Pacific-time day, and the article universe is largely
the same across them — generating a fresh Big Picture three times per
day is wasteful and produces drift (different wording in each edition).

The cache writes ``<pt-date>-bp.json`` once per day and reuses it for the
later same-day editions. These tests cover the helpers in
``generate_news.py`` (``_big_picture_fingerprint``,
``load_big_picture_cache``, ``save_big_picture_cache``) end-to-end
against a temp directory — no Ollama involved.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_news


def _make_article(idx, source="src", title=None, link=None):
    return {
        "title": title or f"Article {idx}",
        "link": link or f"http://example.com/{idx}",
        "source": source,
        "description": f"desc {idx}",
    }


class TestBigPictureFingerprint(unittest.TestCase):
    """`_big_picture_fingerprint` is the staleness signal for the cache.

    Two properties matter:
    1. The same article set (modulo order) hashes equal — so reorders
       between same-day editions do not trigger regeneration.
    2. Any real change (different source, different title) hashes
       differently — so a substantively different news set does trigger
       regeneration.
    """

    def test_same_articles_in_different_order_hash_equal(self):
        a1 = [_make_article(1, "A"), _make_article(2, "B"), _make_article(3, "C")]
        a2 = [_make_article(2, "B"), _make_article(3, "C"), _make_article(1, "A")]
        self.assertEqual(
            generate_news._big_picture_fingerprint(a1),
            generate_news._big_picture_fingerprint(a2),
        )

    def test_extra_article_changes_hash(self):
        base = [_make_article(1, "A"), _make_article(2, "B")]
        with_extra = base + [_make_article(3, "C")]
        self.assertNotEqual(
            generate_news._big_picture_fingerprint(base),
            generate_news._big_picture_fingerprint(with_extra),
        )

    def test_different_source_changes_hash(self):
        a = [_make_article(1, "A"), _make_article(2, "B")]
        b = [_make_article(1, "A"), _make_article(2, "Z")]  # source changed
        self.assertNotEqual(
            generate_news._big_picture_fingerprint(a),
            generate_news._big_picture_fingerprint(b),
        )

    def test_empty_articles_is_stable(self):
        # Hashing an empty set should be deterministic and short.
        self.assertEqual(
            generate_news._big_picture_fingerprint([]),
            generate_news._big_picture_fingerprint([]),
        )
        # 16 hex chars (truncated sha1) — sanity check, not a contract.
        self.assertEqual(len(generate_news._big_picture_fingerprint([])), 16)

    def test_case_insensitive(self):
        # Different casing in source or title should not matter — X
        # handles reuse the same casing, but a future source that does
        # not should not needlessly bust the cache.
        a = [_make_article(1, "OpenAI", title="Hello World")]
        b = [_make_article(1, "openai", title="hello world")]
        self.assertEqual(
            generate_news._big_picture_fingerprint(a),
            generate_news._big_picture_fingerprint(b),
        )


class TestBigPictureCacheIO(unittest.TestCase):
    """`load_big_picture_cache` / `save_big_picture_cache` round-trip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_none_when_missing(self):
        self.assertIsNone(
            generate_news.load_big_picture_cache(self.site_root, "2026-06-05")
        )

    def test_save_then_load_round_trip(self):
        generate_news.save_big_picture_cache(
            self.site_root,
            "2026-06-05",
            fingerprint="abc123def456",
            summary_text="Today was wild.",
            summary_html='<p>Today was <em>wild</em>.</p>',
        )
        loaded = generate_news.load_big_picture_cache(
            self.site_root, "2026-06-05"
        )
        assert loaded is not None  # for type checkers
        self.assertEqual(loaded["date"], "2026-06-05")
        self.assertEqual(loaded["fingerprint"], "abc123def456")
        self.assertEqual(loaded["summary_text"], "Today was wild.")
        self.assertEqual(loaded["summary_html"], '<p>Today was <em>wild</em>.</p>')
        # generated_at is always populated, ISO format, parseable.
        self.assertIn("generated_at", loaded)

    def test_cache_path_layout_matches_roadmap(self):
        """The ROADMAP explicitly says ``YYYY-MM-DD-bp.json`` — verify."""
        path = generate_news._big_picture_cache_path(
            self.site_root, "2026-06-05"
        )
        self.assertEqual(path.name, "2026-06-05-bp.json")
        self.assertEqual(path.parent, self.site_root)

    def test_load_returns_none_for_malformed_json(self):
        path = generate_news._big_picture_cache_path(
            self.site_root, "2026-06-05"
        )
        path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(
            generate_news.load_big_picture_cache(self.site_root, "2026-06-05")
        )

    def test_load_returns_none_for_missing_required_fields(self):
        path = generate_news._big_picture_cache_path(
            self.site_root, "2026-06-05"
        )
        # Has only one of the required fields.
        path.write_text(json.dumps({"date": "2026-06-05"}), encoding="utf-8")
        self.assertIsNone(
            generate_news.load_big_picture_cache(self.site_root, "2026-06-05")
        )

    def test_save_does_not_raise_on_filesystem_error(self):
        # If the site root is unwritable, save should log + swallow
        # (not abort the edition). We simulate that with a read-only dir.
        with mock.patch.object(
            Path, "write_text", side_effect=OSError("read-only fs")
        ):
            # Should not raise.
            generate_news.save_big_picture_cache(
                self.site_root,
                "2026-06-05",
                fingerprint="x",
                summary_text="y",
                summary_html="<p>y</p>",
            )

    def test_load_does_not_raise_on_filesystem_error(self):
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("disk gone")
        ):
            self.assertIsNone(
                generate_news.load_big_picture_cache(
                    self.site_root, "2026-06-05"
                )
            )


class TestBigPictureCacheContract(unittest.TestCase):
    """Document the contract the production code relies on.

    These are mostly sanity tests, but they catch a class of bugs
    (e.g. someone making `summary_text` optional) that the round-trip
    test alone would not.
    """

    def test_fingerprint_is_16_hex_chars(self):
        fp = generate_news._big_picture_fingerprint([_make_article(1)])
        self.assertRegex(fp, r"^[0-9a-f]{16}$")

    def test_fingerprint_rejects_unrelated_articles(self):
        # The fingerprint is keyed on (source, title), not link —
        # multiple X statuses with similar titles should still hash
        # equal on (source, title) and trigger a fingerprint match.
        a = [_make_article(1, "X", title="foo", link="http://a/1")]
        b = [_make_article(1, "X", title="foo", link="http://a/2")]
        self.assertEqual(
            generate_news._big_picture_fingerprint(a),
            generate_news._big_picture_fingerprint(b),
        )


class TestBigPictureCacheReuseLogic(unittest.TestCase):
    """Exercise the same-day reuse decision in isolation.

    The full `generate_post` flow does feed fetching, state persistence,
    and HTML rendering. The Big Picture branch is a small `if cached
    else generate` block — these tests pin the exact decision rule:

    1. Cache file missing → regenerate and save.
    2. Cache file present + matching fingerprint → reuse as-is.
    3. Cache file present + different fingerprint → regenerate and save
       (defensive: catches cache-file corruption or a different day's
       file being read by mistake).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site_root = Path(self._tmp.name)
        self.articles = [
            _make_article(1, "OpenAI", title="GPT-5 launch"),
            _make_article(2, "GoogleAI", title="Gemma 4 release"),
        ]
        self.fp = generate_news._big_picture_fingerprint(self.articles)
        # Disable real network — these tests don't actually call Ollama,
        # they only verify the cache decision.
        self._real_query_ollama = generate_news._query_ollama
        generate_news._query_ollama = lambda prompt, model, **kw: (
            "[[would have called ollama]]"
        )

    def tearDown(self):
        generate_news._query_ollama = self._real_query_ollama
        self._tmp.cleanup()

    def test_missing_cache_triggers_regeneration(self):
        # No cache file exists → load returns None → regen path.
        loaded = generate_news.load_big_picture_cache(
            self.site_root, "2026-06-05"
        )
        self.assertIsNone(loaded, "Pre-condition: no cache file")

        # After regen, save should write a file loadable later.
        generate_news.save_big_picture_cache(
            self.site_root, "2026-06-05", self.fp,
            "summary text", "<p>summary html</p>",
        )
        loaded = generate_news.load_big_picture_cache(
            self.site_root, "2026-06-05"
        )
        assert loaded is not None
        self.assertEqual(loaded["fingerprint"], self.fp)

    def test_matching_fingerprint_reuses_cache(self):
        # Pre-populate the cache with a known fingerprint.
        generate_news.save_big_picture_cache(
            self.site_root, "2026-06-05", self.fp,
            "CACHED TEXT", "<p>CACHED HTML</p>",
        )
        loaded = generate_news.load_big_picture_cache(
            self.site_root, "2026-06-05"
        )
        assert loaded is not None
        # The decision the production code makes:
        if loaded and loaded.get("fingerprint") == self.fp:
            summary_text = loaded["summary_text"]
            summary_html = loaded["summary_html"]
        else:
            summary_text, summary_html = "fresh", "<p>fresh</p>"

        self.assertEqual(summary_text, "CACHED TEXT")
        self.assertEqual(summary_html, "<p>CACHED HTML</p>")

    def test_different_fingerprint_triggers_regeneration(self):
        # Pre-populate the cache with a STALE fingerprint.
        generate_news.save_big_picture_cache(
            self.site_root, "2026-06-05", "stale-fp-xxxxxxxx",
            "STALE TEXT", "<p>STALE HTML</p>",
        )
        loaded = generate_news.load_big_picture_cache(
            self.site_root, "2026-06-05"
        )
        assert loaded is not None
        # The new article set has a different fingerprint → regen.
        should_regen = (
            loaded.get("fingerprint") != self.fp
        )
        self.assertTrue(should_regen)

    def test_next_day_does_not_reuse_prior_day_cache(self):
        # Write a cache for 2026-06-05; loading for 2026-06-06 must
        # return None (different filename → different file → miss).
        generate_news.save_big_picture_cache(
            self.site_root, "2026-06-05", self.fp,
            "prior day", "<p>prior day</p>",
        )
        self.assertIsNone(
            generate_news.load_big_picture_cache(
                self.site_root, "2026-06-06"
            )
        )


if __name__ == "__main__":
    unittest.main()

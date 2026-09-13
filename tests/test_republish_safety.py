"""Tests for republish safety and citation-form repair.

Two defects, both found live on 2026-09-13 after an off-schedule cron fire
(02:45 UTC) hit the republish path in ``generate_post``:

1. **A republish moved a live permalink.** The frontmatter ``date:`` was
   re-derived from "now" instead of being carried over from the post being
   republished. Jekyll computes a post's URL from that date, so rewriting
   ``2026-09-12 09:49:19 -0700`` as ``2026-09-12 19:41:17 -0700`` moved
   ``/news/2026/09/12/Morning/`` to ``/news/2026/09/13/Morning/`` and 404'd the
   old URL. A republish must *reproduce* a post, not re-derive it as if new.

2. **A republish silently degraded citations.** Re-running the Big Picture LLM
   call produced ``(Source, 22, 24, 29)`` instead of the documented
   ``(Source Name: ID)``. ``linkify_summary`` only understood the colon form, so
   nine resolved links became bare unlinked integers with no reference list.
   The model drifts under a strict prompt, so the renderer has to cope.

Both are covered here so the republish path is no longer the untested corner it
was (``grep -r republish tests/`` previously returned nothing).
"""

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import generate_news  # noqa: E402
from generate_news import (  # noqa: E402
    _read_post_frontmatter_date,
    linkify_summary,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def make_articles(n: int) -> list[dict]:
    sources = ["FT AI", "OpenAI", "arXiv", "NYT AI", "AnthropicAI"]
    return [
        {"link": f"https://example.com/{i}", "source": sources[(i - 1) % len(sources)],
         "title": f"Story {i}"}
        for i in range(1, n + 1)
    ]


class TestCollapsedCitationRepair(unittest.TestCase):
    """Every citation form the model actually emits must resolve to links.

    The real production form is ``(Source: 32, 37, 45)`` -- one label with a
    colon, followed by bare indices -- captured from the live Big Picture text
    on 2026-09-13. All three forms are asserted here.
    """

    def setUp(self):
        self.articles = make_articles(50)

    @staticmethod
    def _bare_indices_after_links(html: str) -> list[str]:
        """Unlinked integers still sitting in a citation group.

        The production symptom was ``Source</a>, 22, 24, 29`` -- a resolved link
        followed by bare numbers. This catches that shape directly, whatever the
        form, instead of asserting on one exact rendering.
        """
        return re.findall(r'</a>,\s*(\d+)', html)

    def test_documented_colon_form_still_works(self):
        out = linkify_summary("(Source: 1, Source: 2)", self.articles)
        self.assertEqual(out.count("<a href="), 2)
        self.assertIn("https://example.com/1", out)
        self.assertEqual(self._bare_indices_after_links(out), [])

    def test_real_production_form_colon_plus_bare_indices(self):
        """(Source: 32, 37, 45) -- the form that broke the live Morning post."""
        out = linkify_summary("(Source: 32, 37, 45)", self.articles)
        self.assertEqual(out.count("<a href="), 3)
        for i in (32, 37, 45):
            self.assertIn(f"https://example.com/{i}", out)
        self.assertEqual(self._bare_indices_after_links(out), [])

    def test_colon_plus_pair(self):
        out = linkify_summary("(Source: 1, 4)", self.articles)
        self.assertEqual(out.count("<a href="), 2)
        self.assertEqual(self._bare_indices_after_links(out), [])

    def test_collapsed_form_without_colon_is_resolved(self):
        out = linkify_summary("(Source, 22, 24, 29)", self.articles)
        self.assertEqual(out.count("<a href="), 3)
        for i in (22, 24, 29):
            self.assertIn(f"https://example.com/{i}", out)
        self.assertEqual(self._bare_indices_after_links(out), [])

    def test_collapsed_form_uses_real_source_names(self):
        """Trailing bare indices get the article's own source, not 'Source'."""
        out = linkify_summary("(Source, 1, 2, 3)", self.articles)
        for name in ("FT AI", "OpenAI", "arXiv"):
            self.assertIn(f">{name}</a>", out)

    def test_no_bare_integers_survive_in_the_production_text(self):
        """The literal snippet from the broken post."""
        out = linkify_summary("(Source, 22, 24, 29, 32, 37, 39, 44, 45)", self.articles)
        self.assertNotRegex(out, r"Source,\s*22")
        self.assertEqual(out.count("<a href="), 8)
        self.assertEqual(self._bare_indices_after_links(out), [])

    def test_whole_real_big_picture_paragraph_fully_resolved(self):
        """The exact text shape observed live, all groups in one string."""
        raw = (
            "solving math problems (Source: 39) and agentic systems "
            "proliferate (Source: 32, 37, 45), leaders sound alarms "
            "(Source: 19, 24, 31)."
        )
        out = linkify_summary(raw, self.articles)
        self.assertEqual(out.count("<a href="), 7, out)
        self.assertEqual(self._bare_indices_after_links(out), [])

    def test_out_of_range_indices_left_alone(self):
        """Never invent a link for an index we cannot resolve."""
        for text in ("(Source, 22, 99)", "(Source: 1, 99)", "(Source: 99)"):
            out = linkify_summary(text, self.articles)
            self.assertNotIn("<a href=", out, text)
            self.assertEqual(out, text)

    def test_ordinary_prose_parentheses_untouched(self):
        for text in ("(Some prose aside)", "(Note, this is fine)",
                     "(See the appendix)", "(as discussed, above)"):
            self.assertEqual(linkify_summary(text, self.articles), text)

    def test_leading_index_without_label_untouched(self):
        text = "(1, 2, 3)"
        self.assertEqual(linkify_summary(text, self.articles), text)

    def test_single_element_parens_untouched(self):
        self.assertEqual(linkify_summary("(42)", self.articles), "(42)")

    def test_empty_article_list_never_links(self):
        out = linkify_summary("(Source, 1, 2)", [])
        self.assertNotIn("<a href=", out)

    def test_mixed_forms_in_one_summary(self):
        out = linkify_summary("(Source: 1) then (Source, 2, 3)", self.articles)
        self.assertEqual(out.count("<a href="), 3)

    def test_nitter_links_still_converted(self):
        arts = [{"link": "https://nitter.net/AnthropicAI/status/123", "source": "AnthropicAI"}]
        out = linkify_summary("(Source, 1)", arts)
        self.assertIn("x.com", out)
        self.assertNotIn("nitter.net", out)


class TestFrontmatterDateReader(unittest.TestCase):
    """The helper that keeps a republish's permalink stable."""

    def _post(self, body: str) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        site = Path(td.name)
        (site / "_posts").mkdir(parents=True)
        p = site / "_posts" / "2026-09-12-Morning.html"
        p.write_text(body, encoding="utf-8")
        return p

    def test_reads_jekyll_datetime_with_offset(self):
        p = self._post("---\nlayout: post\ndate: 2026-09-12 09:49:19 -0700\n---\n")
        dt = _read_post_frontmatter_date(p)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M:%S"), "2026-09-12 09:49:19")
        self.assertIsNotNone(dt.tzinfo)

    def test_missing_file_returns_none(self):
        self.assertIsNone(_read_post_frontmatter_date(Path("/tmp/does-not-exist-xyz.html")))

    def test_no_date_field_returns_none(self):
        p = self._post("---\nlayout: post\ntitle: hi\n---\n")
        self.assertIsNone(_read_post_frontmatter_date(p))

    def test_garbage_date_returns_none(self):
        p = self._post("---\ndate: not a date at all\n---\n")
        self.assertIsNone(_read_post_frontmatter_date(p))

    def test_date_only_form_returned(self):
        p = self._post("---\ndate: 2026-09-12\n---\n")
        dt = _read_post_frontmatter_date(p)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.strftime("%Y-%m-%d"), "2026-09-12")


class TestRepublishPreservesPermalink(unittest.TestCase):
    """End-to-end: republishing must not re-stamp the frontmatter date."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.site = Path(self.td.name)
        (self.site / "_posts").mkdir()

    def test_generate_post_keeps_original_date_on_republish(self):
        """Republish an existing post; its date (and so its URL) must not move."""
        post = self.site / "_posts" / "2026-09-12-Morning.html"
        original = "2026-09-12 09:49:19 -0700"
        post.write_text(
            f"---\nlayout: post\ndate: {original}\n---\n<body>old</body>\n",
            encoding="utf-8",
        )
        # The date the helper recovers must be the one Jekyll will use.
        dt = _read_post_frontmatter_date(post)
        self.assertEqual(
            dt.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M:%S %z"), original
        )
        # And it must land on the same calendar day as the original permalink.
        self.assertEqual(dt.astimezone(PACIFIC).strftime("%Y/%m/%d"), "2026/09/12")

    def test_republish_of_a_post_crossing_utc_midnight_stays_put(self):
        """The production case: 19:41 PDT is 02:41 UTC the NEXT day.

        Using "now" (UTC) moved the post a day forward. The original local
        timestamp must win, or the URL changes.
        """
        post = self.site / "_posts" / "2026-09-12-Evening.html"
        post.write_text(
            "---\nlayout: post\ndate: 2026-09-12 19:01:33 -0700\n---\n",
            encoding="utf-8",
        )
        dt = _read_post_frontmatter_date(post)
        local = dt.astimezone(PACIFIC)
        utc = dt.astimezone(timezone.utc)
        self.assertEqual(local.strftime("%Y-%m-%d"), "2026-09-12")
        self.assertEqual(utc.strftime("%Y-%m-%d"), "2026-09-13", "crosses midnight UTC")
        # Permalink follows the Jekyll/Pacific date, which is stable.
        self.assertEqual(local.strftime("%Y/%m/%d"), "2026/09/12")


class TestRepublishIsWired(unittest.TestCase):
    """Guard the wiring itself, since this path had no tests at all."""

    def test_generate_post_reads_the_original_date_when_republishing(self):
        src = (PROJECT_ROOT / "generate_news.py").read_text(encoding="utf-8")
        self.assertIn("_read_post_frontmatter_date(filepath)", src)
        # The call must sit under a republish guard, not run on fresh posts.
        idx = src.index("_read_post_frontmatter_date(filepath)")
        window = src[max(0, idx - 600):idx]
        self.assertIn("if republish:", window)

    def test_fresh_post_still_uses_now(self):
        """The fix must not freeze fresh editions to any old date."""
        src = (PROJECT_ROOT / "generate_news.py").read_text(encoding="utf-8")
        # Anchor on the actual date-preservation call, not the first
        # "if republish:" in the file (the fetch-skip branch matches earlier).
        idx = src.index("_read_post_frontmatter_date(filepath)")
        window = src[idx:idx + 400]
        self.assertIn("post_now = original_dt", window)
        # post_now is still initialised from now() above the guard, so fresh
        # runs are unaffected.
        before = src[max(0, idx - 900):idx]
        self.assertIn("datetime.now", before)


class TestCachedBigPictureIsReRendered(unittest.TestCase):
    """A cached day must not pin a stale renderer.

    The Big Picture cache stores pre-rendered ``summary_html``. Reusing it
    verbatim meant a citation-parsing fix could never reach a cached PT day: the
    live Morning post kept its bare ``Source</a>, 22, 24, 29`` indices even with
    the parser fixed, because the HTML came straight from the cache. Rendering
    is deterministic and needs no LLM call, so the cache's *text* is re-rendered
    on reuse.
    """

    def test_reuse_path_rerenders_from_text(self):
        src = (PROJECT_ROOT / "generate_news.py").read_text(encoding="utf-8")
        idx = src.index("Reusing 'The Big Picture' from cache")
        window = src[idx:idx + 900]
        self.assertIn("global_summary_text = cached[\"summary_text\"]", window)
        self.assertIn("linkify_summary(global_summary_text, all_articles)", window)
        self.assertNotIn('global_summary_html = cached["summary_html"]', window)

    def test_cached_stale_html_would_still_be_broken(self):
        """Documents the bug: the cached HTML genuinely contains bare indices."""
        stale_html = '(<a href="https://x.com/1">Source</a>, 22, 24, 29)'
        self.assertTrue(re.search(r'</a>,\s*\d', stale_html))

    def test_rerender_of_that_text_resolves_everything(self):
        """Same text, re-rendered, is fully linked (0 bare indices)."""
        articles = make_articles(50)
        text = "proliferate (Source: 32, 37, 45) and alarms (Source: 19, 24, 31)"
        out = linkify_summary(text, articles)
        self.assertEqual(re.findall(r'</a>,\s*\d+', out), [])
        self.assertEqual(out.count("<a href="), 6)


if __name__ == "__main__":
    unittest.main()

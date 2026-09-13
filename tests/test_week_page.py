"""Tests for the /week/ page (tools/build_week_page.py) and the /now/ page.

Covers:
- extract_big_picture() pulls the Big Picture paragraph and strips HTML.
- collect_entries() windows by days and orders reverse-chronologically.
- render_page() emits front matter, a permalink, and escaped content.
- build() writes week.html atomically and never raises on a missing _posts.
- now.html is a Jekyll page with front matter and a /now/ permalink.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from build_week_page import (  # noqa: E402
    DEFAULT_DAYS,
    OUTPUT_FILE,
    audio_player_html,
    build,
    collect_entries,
    extract_big_picture,
    extract_big_picture_audio,
    read_baseurl,
    read_post_date,
    render_page,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
BASEURL = "/ai-news"

SAMPLE_POST = """---
layout: post
title: "AI News Digest — Morning Edition"
date: 2026-09-12 09:49:19 -0700
categories: news digest
---

<h3 style="margin-top:0;">🌍 The Big Picture</h3>
<div class="audio-player"><audio controls><source src="/ai-news/assets/audio/2026-09-12-Morning/big-picture.mp3" type="audio/mpeg"></audio></div>
<p>AI is <a href="https://x.com">transforming</a> everything.</p>
"""


class TestExtractBigPicture(unittest.TestCase):

    def test_extracts_and_strips_html(self):
        out = extract_big_picture(SAMPLE_POST)
        self.assertEqual(out, "AI is transforming everything.")

    def test_no_big_picture_returns_empty(self):
        self.assertEqual(extract_big_picture("<h2>News</h2><p>hi</p>"), "")

    def test_extracts_big_picture_audio(self):
        src = extract_big_picture_audio(SAMPLE_POST)
        self.assertEqual(src, "/ai-news/assets/audio/2026-09-12-Morning/big-picture.mp3")

    def test_no_audio_returns_empty(self):
        post = SAMPLE_POST.replace(
            '<source src="/ai-news/assets/audio/2026-09-12-Morning/big-picture.mp3" type="audio/mpeg">',
            "",
        )
        self.assertEqual(extract_big_picture_audio(post), "")

    def test_read_post_date(self):
        dt = read_post_date(SAMPLE_POST)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 9)

    def test_read_post_date_missing(self):
        self.assertIsNone(read_post_date("---\nlayout: post\n---\n"))


class TestReadBaseurl(unittest.TestCase):

    def _site_root(self, tmp: Path, baseurl: str | None) -> Path:
        root = tmp / "site"
        root.mkdir()
        if baseurl is not None:
            (root / "_config.yml").write_text(f'baseurl: "{baseurl}"\n', encoding="utf-8")
        return root

    def test_reads_baseurl(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_baseurl(self._site_root(Path(td), "/ai-news")), "/ai-news")

    def test_strips_trailing_slash_and_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_baseurl(self._site_root(Path(td), "/ai-news/")), "/ai-news")

    def test_empty_baseurl(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_baseurl(self._site_root(Path(td), "")), "")

    def test_missing_config_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_baseurl(self._site_root(Path(td), None)), "/ai-news")


class TestAudioPlayerHtml(unittest.TestCase):

    def test_renders_audio_with_aria_label(self):
        html = audio_player_html("/ai-news/assets/audio/x/big-picture.mp3")
        self.assertIn("<audio", html)
        self.assertIn('aria-label="Audio summary of Big Picture summary"', html)
        self.assertIn('src="/ai-news/assets/audio/x/big-picture.mp3"', html)


class TestCollectEntries(unittest.TestCase):

    def _posts_dir(self, tmp: Path) -> Path:
        d = tmp / "_posts"
        d.mkdir()
        return d

    def test_windows_and_orders(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._posts_dir(Path(td))
            older = SAMPLE_POST.replace("2026-09-12 09:49:19 -0700",
                                        "2026-09-05 09:00:00 -0700")
            (d / "2026-09-05-Morning.html").write_text(older, encoding="utf-8")
            (d / "2026-09-12-Morning.html").write_text(SAMPLE_POST, encoding="utf-8")
            entries = collect_entries(d, days=7, now=NOW, baseurl=BASEURL)
            # The 09-05 entry is outside the 7-day window.
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["title"], "AI News Digest — Morning Edition")

    def test_url_includes_baseurl(self):
        """Edition links must carry the /ai-news baseurl (the live 404 bug)."""
        with tempfile.TemporaryDirectory() as td:
            d = self._posts_dir(Path(td))
            (d / "2026-09-12-Morning.html").write_text(SAMPLE_POST, encoding="utf-8")
            entries = collect_entries(d, days=7, now=NOW, baseurl=BASEURL)
            self.assertEqual(entries[0]["url"], "/ai-news/news/2026/09/12/Morning/")

    def test_entry_carries_audio(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._posts_dir(Path(td))
            (d / "2026-09-12-Morning.html").write_text(SAMPLE_POST, encoding="utf-8")
            entries = collect_entries(d, days=7, now=NOW, baseurl=BASEURL)
            self.assertEqual(entries[0]["audio"],
                             "/ai-news/assets/audio/2026-09-12-Morning/big-picture.mp3")

    def test_orders_reverse_chronological(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._posts_dir(Path(td))
            morning = SAMPLE_POST
            evening = (SAMPLE_POST
                       .replace("09:49:19 -0700", "19:00:00 -0700")
                       .replace("Morning Edition", "Evening Edition"))
            (d / "2026-09-12-Morning.html").write_text(morning, encoding="utf-8")
            (d / "2026-09-12-Evening.html").write_text(evening, encoding="utf-8")
            entries = collect_entries(d, days=7, now=NOW, baseurl=BASEURL)
            self.assertEqual(len(entries), 2)
            self.assertIn("Evening", entries[0]["title"], "newest edition first")

    def test_missing_posts_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            entries = collect_entries(Path(td) / "_posts", days=7, now=NOW, baseurl=BASEURL)
            self.assertEqual(entries, [])


class TestRenderPage(unittest.TestCase):

    def _entries(self):
        return [{
            "title": "AI News Digest — Morning Edition",
            "url": "/news/2026/09/12/Morning/",
            "date": datetime(2026, 9, 12, 9, 49, tzinfo=timezone.utc),
            "bp": "AI is transforming everything.",
        }]

    def test_front_matter_and_permalink(self):
        html = render_page(self._entries(), days=7, now=NOW)
        self.assertTrue(html.startswith("---\n"))
        self.assertIn("layout: page", html)
        self.assertIn("permalink: /week/", html)
        self.assertIn("title: This Week", html)

    def test_no_h1_in_body(self):
        html = render_page(self._entries(), days=7, now=NOW)
        body = html.split("---\n", 2)[2]
        self.assertNotIn("<h1", body.lower())

    def test_escapes_content(self):
        entries = [{
            "title": "Evil <script>",
            "url": "/news/x/",
            "date": datetime(2026, 9, 12, tzinfo=timezone.utc),
            "bp": 'bad <script>" text',
        }]
        html = render_page(entries, days=7, now=NOW)
        self.assertNotIn("<script>", html)

    def test_empty_state_renders_placeholder(self):
        html = render_page([], days=7, now=NOW)
        self.assertIn("week-empty", html)


class TestBuildEndToEnd(unittest.TestCase):

    def test_build_writes_page_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "_posts").mkdir()
            (root / "_posts" / "2026-09-12-Morning.html").write_text(
                SAMPLE_POST, encoding="utf-8")
            out = build(root, now=NOW)
            self.assertEqual(out.name, OUTPUT_FILE)
            self.assertTrue((root / OUTPUT_FILE).exists())
            self.assertFalse((root / "week.html.tmp").exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("AI is transforming everything.", content)

    def test_build_no_posts_dir_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            out = build(Path(td), now=NOW)
            self.assertIn("week-empty", out.read_text(encoding="utf-8"))


class TestNowPage(unittest.TestCase):

    def test_now_page_exists_with_front_matter(self):
        p = PROJECT_ROOT / "now.html"
        self.assertTrue(p.exists(), "now.html missing from site root")
        content = p.read_text()
        self.assertIn("layout: page", content)
        self.assertIn("permalink: /now/", content)
        self.assertIn("title: Now", content)

    def test_now_page_filters_by_today(self):
        content = (PROJECT_ROOT / "now.html").read_text()
        # Must compare each post's day to the build time's day.
        self.assertIn('site.time | date: "%Y-%m-%d"', content)
        self.assertIn('post.date | date: "%Y-%m-%d"', content)

    def test_now_page_has_empty_state(self):
        content = (PROJECT_ROOT / "now.html").read_text()
        self.assertIn("No editions published yet today", content)


if __name__ == "__main__":
    unittest.main()

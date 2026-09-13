"""Tests for the /news/ archive index page (news.html).

The top-level /news/ URL was a 404 on the live site: the day-index generator
creates /news/:year/:month/:day/ pages but nothing produces /news/index.html,
yet the home page links to /news/ as the "archive". news.html is a static
Liquid page that fills that gap by listing every edition grouped by day.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jekyll_build import build_site

SITE_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = SITE_ROOT / "_site"


class TestNewsIndexPage(unittest.TestCase):

    def test_news_page_exists_with_front_matter(self):
        p = SITE_ROOT / "news.html"
        self.assertTrue(p.exists(), "news.html missing from site root")
        content = p.read_text()
        self.assertIn("layout: page", content)
        self.assertIn("permalink: /news/", content)

    def test_news_page_groups_by_day(self):
        content = (SITE_ROOT / "news.html").read_text()
        self.assertIn('post.date | date: "%Y-%m-%d"', content)
        self.assertIn("news-day-group", content)

    def test_news_page_links_to_day_indices(self):
        content = (SITE_ROOT / "news.html").read_text()
        self.assertIn('/news/', content)
        self.assertIn('post_date | date: "%Y/%m/%d"', content)


class TestNewsIndexBuild(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build_result = build_site()

    def test_jekyll_build_succeeds(self):
        self.assertEqual(self.build_result.returncode, 0,
                         f"Jekyll build failed: {self.build_result.stderr[-500:]}")

    def test_news_index_page_generated(self):
        p = SITE_DIR / "news" / "index.html"
        self.assertTrue(p.exists(), "/news/index.html not generated")

    def test_news_index_has_day_groups(self):
        p = SITE_DIR / "news" / "index.html"
        content = p.read_text()
        groups = re.findall(r'class="news-day-group"', content)
        self.assertGreater(len(groups), 5,
                           f"expected many day groups, found {len(groups)}")

    def test_news_index_links_to_editions(self):
        p = SITE_DIR / "news" / "index.html"
        content = p.read_text()
        links = re.findall(r'href="[^"]*/news/\d{4}/\d{2}/\d{2}/[A-Za-z]+/"', content)
        self.assertGreater(len(links), 0, "no edition links on /news/ page")


if __name__ == "__main__":
    unittest.main()

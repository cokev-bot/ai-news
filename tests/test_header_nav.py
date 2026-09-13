"""Tests that the site header nav is pinned to top-level pages only.

Minima's header renders one nav link per page in ``site.pages`` by default.
The day-index generator creates a page per day (titled "AI News Digest —
<date>"), which flooded the header with hundreds of date links. The fix pins
``header_pages`` in ``_config.yml`` so the nav lists only the real top-level
pages, and no day-index "AI News Digest — <date>" links leak into it.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jekyll_build import build_site

SITE_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = SITE_ROOT / "_site"


class TestHeaderPagesConfig(unittest.TestCase):

    def test_config_pins_header_pages(self):
        content = (SITE_ROOT / "_config.yml").read_text()
        self.assertIn("header_pages:", content,
                      "_config.yml must pin header_pages so the day-index "
                      "pages don't flood the nav")
        self.assertIn("source-status.html", content)
        self.assertIn("now.html", content)
        self.assertIn("week.html", content)

    def test_header_pages_excludes_day_index_titles(self):
        """No 'AI News Digest — <date>' page should be a nav target."""
        content = (SITE_ROOT / "_config.yml").read_text()
        # The day-index pages are generated, not authored files, so none of
        # their paths should appear in the header_pages list.
        self.assertNotIn("news/2026/", content)


class TestHeaderNavBuild(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build_result = build_site()

    def test_jekyll_build_succeeds(self):
        self.assertEqual(self.build_result.returncode, 0,
                         f"Jekyll build failed: {self.build_result.stderr[-500:]}")

    def _nav_links(self, page: Path) -> list[str]:
        html = page.read_text()
        return re.findall(r'<a class="page-link"[^>]*>(.*?)</a>', html)

    def test_source_status_header_has_no_date_links(self):
        page = SITE_DIR / "source-status" / "index.html"
        if not page.exists():
            self.skipTest("source-status page not built")
        links = self._nav_links(page)
        self.assertGreater(len(links), 0, "nav should still have some links")
        for link in links:
            self.assertNotRegex(
                link, r"AI News Digest —",
                f"date page leaked into header nav: {link!r}")

    def test_edition_post_header_has_no_date_links(self):
        page = SITE_DIR / "news" / "2026" / "06" / "12" / "Morning" / "index.html"
        if not page.exists():
            self.skipTest("edition post not built")
        links = self._nav_links(page)
        for link in links:
            self.assertNotRegex(link, r"AI News Digest —")

    def test_nav_contains_top_level_pages(self):
        page = SITE_DIR / "source-status" / "index.html"
        if not page.exists():
            self.skipTest("source-status page not built")
        links = self._nav_links(page)
        texts = [re.sub(r"<[^>]+>", "", l) for l in links]
        for expected in ("Now", "Archive", "This Week", "Source Status"):
            self.assertIn(expected, texts, f"nav missing {expected!r}")


if __name__ == "__main__":
    unittest.main()

"""Tests for previous/next edition navigation (the local _layouts/post.html).

Covers:
- The post layout override exists and is structurally the Minima post layout
  (so we don't silently drop the theme's own post rendering).
- It renders previous/next links from Jekyll's page.previous / page.next.
- A built edition post contains the navigation block when siblings exist.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jekyll_build import build_site

SITE_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = SITE_ROOT / "_site"


class TestPostLayoutExists(unittest.TestCase):

    def test_post_layout_file_exists(self):
        self.assertTrue((SITE_ROOT / "_layouts" / "post.html").exists())

    def test_post_layout_preserves_theme_article_structure(self):
        """The override must still render the Minima post <article> shape."""
        content = (SITE_ROOT / "_layouts" / "post.html").read_text()
        self.assertIn("post h-entry", content)
        self.assertIn("post-title", content)
        self.assertIn("{{ content }}", content)
        self.assertIn("post-content", content)

    def test_post_layout_has_nav_block(self):
        content = (SITE_ROOT / "_layouts" / "post.html").read_text()
        self.assertIn("post-nav", content)
        self.assertIn("page.previous", content)
        self.assertIn("page.next", content)
        self.assertIn('rel="prev"', content)
        self.assertIn('rel="next"', content)


class TestPostNavBuild(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build_result = build_site()

    def test_jekyll_build_succeeds(self):
        self.assertEqual(self.build_result.returncode, 0,
                         f"Jekyll build failed: {self.build_result.stderr[-500:]}")

    def test_some_post_has_navigation(self):
        """At least one post with an older sibling should render a nav block."""
        # A day with Morning + Afternoon + Evening guarantees middle siblings
        # have both a previous and a next edition.
        candidate = SITE_DIR / "news/2026/06/12/Afternoon/index.html"
        if not candidate.exists():
            self.skipTest("candidate post not present in build")
        content = candidate.read_text()
        self.assertIn("post-nav", content)

    def test_navigation_links_point_to_edition_pages(self):
        """Nav anchors should target /news/.../Edition/ URLs, not the home page."""
        posts = list(SITE_DIR.glob("news/20??/??/??/*/index.html"))
        found_nav = 0
        for p in posts:
            c = p.read_text()
            for m in re.findall(r'class="post-nav"[^>]*>(.*?)</nav>', c, re.DOTALL):
                hrefs = re.findall(r'href="([^"]+)"', m)
                for h in hrefs:
                    self.assertIn("/news/", h, f"nav link {h!r} is not an edition URL")
                    found_nav += 1
        self.assertGreater(found_nav, 0, "no navigation links rendered in any post")


if __name__ == "__main__":
    unittest.main()

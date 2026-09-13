"""Tests for the footer social links override.

The theme's default social.html built a broken "https://github.com/" link from
an empty `github_username` and a dead Twitter link. The local
_includes/social.html override renders `site.github_url` as the repo link and
omits Twitter entirely.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jekyll_build import build_site

SITE_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = SITE_ROOT / "_site"


class TestSocialOverrideExists(unittest.TestCase):

    def test_social_include_exists(self):
        self.assertTrue((SITE_ROOT / "_includes" / "social.html").exists())

    def test_social_uses_github_url_not_username(self):
        content = (SITE_ROOT / "_includes" / "social.html").read_text()
        self.assertIn("site.github_url", content)
        self.assertNotIn("github_username", content)

    def test_social_has_no_twitter(self):
        content = (SITE_ROOT / "_includes" / "social.html").read_text()
        self.assertNotIn("twitter", content.lower())

    def test_config_defines_github_url(self):
        content = (SITE_ROOT / "_config.yml").read_text()
        self.assertIn('github_url: "https://github.com/cokev-bot/ai-news"', content)

    def test_config_has_no_twitter_username(self):
        content = (SITE_ROOT / "_config.yml").read_text()
        self.assertNotIn("twitter_username", content)


class TestSocialBuild(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build_result = build_site()

    def test_jekyll_build_succeeds(self):
        self.assertEqual(self.build_result.returncode, 0,
                         f"Jekyll build failed: {self.build_result.stderr[-500:]}")

    def test_footer_links_to_repo(self):
        page = SITE_DIR / "source-status" / "index.html"
        if not page.exists():
            self.skipTest("source-status not built")
        content = page.read_text()
        self.assertIn('href="https://github.com/cokev-bot/ai-news"', content)

    def test_no_broken_empty_github_link(self):
        page = SITE_DIR / "source-status" / "index.html"
        if not page.exists():
            self.skipTest("source-status not built")
        self.assertNotIn('href="https://github.com/"', page.read_text())

    def test_no_twitter_footer_link(self):
        """No page's footer should render a twitter.com link."""
        for page in [SITE_DIR / "source-status" / "index.html",
                     SITE_DIR / "news" / "2026" / "09" / "12" / "Morning" / "index.html"]:
            if page.exists():
                content = page.read_text()
                # Twitter appears in meta tags (twitter:card) legitimately; the
                # thing we must remove is the social footer link to twitter.com.
                self.assertNotIn('twitter.com/', content,
                                 f"{page} still has a twitter.com footer link")


if __name__ == "__main__":
    unittest.main()

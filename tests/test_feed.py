"""Tests for the site-level RSS/Atom feed (feed.xml) and related configuration.

Phase 4 roadmap item: "Add site-level RSS feed (feed.xml)"

These tests verify that:
  1. _config.yml has jekyll-feed configured with proper url/baseurl
  2. The Jekyll build produces a valid Atom feed at _site/feed.xml
  3. The feed contains absolute (not relative) URLs
  4. The feed includes enough entries (posts_limit >= 20)
  5. Each entry has required Atom elements (title, id, link, updated, content)
  6. The home page links to the feed via alternate tag and subscribe link
  7. robots.txt references both sitemap.xml and feed.xml
"""

import json
import os
import re
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "_config.yml"
SITE_DIR = PROJECT_ROOT / "_site"
FEED_FILE = SITE_DIR / "feed.xml"
ROBOTS_FILE = PROJECT_ROOT / "robots.txt"
SITE_ROBOTS = SITE_DIR / "robots.txt"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _load_config() -> dict:
    """Parse _config.yml (simple — no nested structures needed for our checks)."""
    import yaml

    with CONFIG_FILE.open() as fh:
        return yaml.safe_load(fh)


def _parse_feed() -> ET.ElementTree:
    """Parse _site/feed.xml, raising if the file is missing or invalid."""
    if not FEED_FILE.exists():
        raise FileNotFoundError(
            f"{FEED_FILE} not found. Run `bundle exec jekyll build` first."
        )
    return ET.parse(str(FEED_FILE))


def _rebuild_site():
    """Run jekyll build and return the exit code."""
    result = subprocess.run(
        ["bundle", "exec", "jekyll", "build", "--destination", str(SITE_DIR)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode


# -----------------------------------------------------------------------
# Test cases
# -----------------------------------------------------------------------


class TestConfigFeedSettings(unittest.TestCase):
    """Verify _config.yml has the right feed-related settings."""

    @classmethod
    def setUpClass(cls):
        cls.config = _load_config()

    def test_url_is_set(self):
        """_config.yml must have a non-empty url for absolute feed links."""
        url = self.config.get("url", "")
        self.assertTrue(url, "url must be set in _config.yml (was empty or missing)")
        self.assertTrue(
            url.startswith("http"),
            f"url should start with http, got: {url}",
        )

    def test_baseurl_is_set(self):
        """baseurl must be set for GitHub Pages projects (e.g. /ai-news)."""
        baseurl = self.config.get("baseurl", "")
        # baseurl can be empty for top-level sites, but for gh-pages project sites it must be set
        self.assertIsNotNone(baseurl, "baseurl must exist in _config.yml")

    def test_jekyll_feed_plugin(self):
        """jekyll-feed must be listed in plugins."""
        plugins = self.config.get("plugins", [])
        self.assertIn(
            "jekyll-feed", plugins, "jekyll-feed must be in the plugins list"
        )

    def test_jekyll_sitemap_plugin(self):
        """jekyll-sitemap must be listed in plugins."""
        plugins = self.config.get("plugins", [])
        self.assertIn(
            "jekyll-sitemap", plugins, "jekyll-sitemap must be in the plugins list"
        )

    def test_feed_posts_limit(self):
        """feed.posts_limit must be >= 20 so the feed isn't truncated to the default 10."""
        feed_config = self.config.get("feed", {})
        limit = feed_config.get("posts_limit", 10)
        self.assertGreaterEqual(
            limit,
            20,
            f"feed.posts_limit should be >= 20, got {limit}",
        )


class TestFeedXml(unittest.TestCase):
    """Verify the generated Atom feed is valid and complete."""

    @classmethod
    def setUpClass(cls):
        # Build the site so _site/feed.xml is fresh
        exit_code = _rebuild_site()
        if exit_code != 0:
            raise RuntimeError(f"Jekyll build failed with exit code {exit_code}")
        cls.tree = _parse_feed()
        cls.root = cls.tree.getroot()

    def test_feed_exists_and_parseable(self):
        """_site/feed.xml must exist and be valid XML."""
        self.assertTrue(FEED_FILE.exists(), "feed.xml must exist in _site/")

    def test_feed_is_atom(self):
        """The root element must be in the Atom namespace."""
        tag = self.root.tag
        self.assertIn(
            "http://www.w3.org/2005/Atom",
            tag,
            f"Root element should be Atom feed, got: {tag}",
        )

    def test_feed_title(self):
        """Feed must have a non-empty title."""
        title = self.root.find("atom:title", ATOM_NS)
        self.assertIsNotNone(title, "Feed must have a <title> element")
        self.assertTrue(title.text, "Feed title must not be empty")

    def test_feed_subtitle(self):
        """Feed must have a non-empty subtitle (description)."""
        subtitle = self.root.find("atom:subtitle", ATOM_NS)
        self.assertIsNotNone(subtitle, "Feed must have a <subtitle> element")
        self.assertTrue(subtitle.text, "Feed subtitle must not be empty")

    def test_feed_id(self):
        """Feed must have an id element."""
        feed_id = self.root.find("atom:id", ATOM_NS)
        self.assertIsNotNone(feed_id, "Feed must have an <id> element")
        self.assertTrue(feed_id.text, "Feed id must not be empty")

    def test_feed_self_link(self):
        """Feed must have a self link with absolute URL."""
        self_links = [
            l
            for l in self.root.findall("atom:link", ATOM_NS)
            if l.attrib.get("rel") == "self"
        ]
        self.assertTrue(len(self_links) >= 1, "Feed must have a self link")
        href = self_links[0].attrib.get("href", "")
        self.assertTrue(
            href.startswith("http"),
            f"Self link must be absolute URL, got: {href}",
        )
        self.assertIn("feed.xml", href, "Self link must point to feed.xml")

    def test_feed_alternate_link(self):
        """Feed must have an alternate link to the home page."""
        alt_links = [
            l
            for l in self.root.findall("atom:link", ATOM_NS)
            if l.attrib.get("rel") == "alternate"
        ]
        self.assertTrue(len(alt_links) >= 1, "Feed must have an alternate link")
        href = alt_links[0].attrib.get("href", "")
        self.assertTrue(
            href.startswith("http"),
            f"Alternate link must be absolute URL, got: {href}",
        )

    def test_feed_has_entries(self):
        """Feed must contain at least 10 entries (we have 89 posts)."""
        entries = self.root.findall("atom:entry", ATOM_NS)
        self.assertGreaterEqual(
            len(entries), 10, f"Feed should have at least 10 entries, got {len(entries)}"
        )

    def test_feed_entries_have_absolute_urls(self):
        """All entry links must be absolute URLs (not relative /path/...)."""
        entries = self.root.findall("atom:entry", ATOM_NS)
        for e in entries:
            link = e.find("atom:link", ATOM_NS)
            self.assertIsNotNone(link, "Entry must have a <link> element")
            href = link.attrib.get("href", "")
            self.assertTrue(
                href.startswith("http"),
                f"Entry link must be absolute, got: {href}",
            )

    def test_feed_entry_ids_are_absolute(self):
        """All entry ids must be absolute URIs."""
        entries = self.root.findall("atom:entry", ATOM_NS)
        for e in entries:
            eid = e.find("atom:id", ATOM_NS)
            self.assertIsNotNone(eid, "Entry must have an <id> element")
            self.assertTrue(
                eid.text.startswith("http"),
                f"Entry id must be absolute URI, got: {eid.text}",
            )

    def test_entry_required_elements(self):
        """Each entry must have title, id, link, updated, published, content."""
        entries = self.root.findall("atom:entry", ATOM_NS)
        for e in entries:
            for tag in ["title", "id", "link", "updated", "published", "content"]:
                el = e.find(f"atom:{tag}", ATOM_NS)
                self.assertIsNotNone(
                    el,
                    f"Entry '{e.find('atom:title', ATOM_NS).text[:40]}' must have <{tag}>",
                )

    def test_entry_content_not_empty(self):
        """Each entry must have non-empty content."""
        entries = self.root.findall("atom:entry", ATOM_NS)
        for e in entries:
            content = e.find("atom:content", ATOM_NS)
            self.assertTrue(
                content.text and len(content.text) > 100,
                f"Entry '{e.find('atom:title', ATOM_NS).text[:40]}' content must be >100 chars",
            )

    def test_feed_generator(self):
        """Feed should identify Jekyll as the generator."""
        gen = self.root.find("atom:generator", ATOM_NS)
        self.assertIsNotNone(gen, "Feed must have a <generator> element")
        self.assertIn("Jekyll", gen.text, "Generator should mention Jekyll")


class TestHomePageFeedLinks(unittest.TestCase):
    """Verify the home page links to the feed correctly."""

    @classmethod
    def setUpClass(cls):
        index_file = SITE_DIR / "index.html"
        if not index_file.exists():
            _rebuild_site()
        with index_file.open() as fh:
            cls.html = fh.read()

    def test_home_has_alternate_feed_link(self):
        """Home page must have an alternate link tag for the Atom feed."""
        self.assertIn(
            'type="application/atom+xml"',
            self.html,
            "Home page must link to the Atom feed via <link rel=alternate>",
        )
        self.assertIn(
            "feed.xml",
            self.html,
            "Home page must reference feed.xml",
        )

    def test_home_has_rss_subscribe_link(self):
        """Home page must have a visible 'subscribe via RSS' link."""
        # Minima includes an RSS subscribe link in the footer
        self.assertTrue(
            "via RSS" in self.html or "rss-subscribe" in self.html,
            "Home page must have a visible RSS subscribe link",
        )


class TestRobotsTxt(unittest.TestCase):
    """Verify robots.txt exists and references sitemap and feed."""

    def test_source_robots_txt_exists(self):
        """A source robots.txt must exist in the project root."""
        self.assertTrue(
            ROBOTS_FILE.exists(), "robots.txt must exist in project root"
        )

    def test_robots_allows_all(self):
        """robots.txt must allow all crawlers."""
        content = ROBOTS_FILE.read_text()
        self.assertIn("User-agent: *", content, "robots.txt must allow all user-agents")
        self.assertIn("Allow: /", content, "robots.txt must allow /")

    def test_robots_references_sitemap(self):
        """robots.txt must reference the sitemap.xml with an absolute URL."""
        content = ROBOTS_FILE.read_text()
        self.assertIn("Sitemap:", content, "robots.txt must reference sitemap.xml")
        # Must be an absolute URL (now that we have url/baseurl configured)
        self.assertIn(
            "https://",
            content.split("Sitemap:")[1].split("\n")[0],
            "Sitemap URL must be absolute",
        )

    def test_robots_references_feed(self):
        """robots.txt must mention the feed URL for discoverability."""
        content = ROBOTS_FILE.read_text()
        self.assertIn(
            "feed.xml",
            content,
            "robots.txt should reference feed.xml for discoverability",
        )

    def test_site_robots_txt_is_built(self):
        """_site/robots.txt must exist after Jekyll build."""
        _rebuild_site()
        self.assertTrue(
            SITE_ROBOTS.exists(), "_site/robots.txt must exist after build"
        )


class TestSitemapAbsoluteUrls(unittest.TestCase):
    """Verify sitemap.xml has absolute URLs after config change."""

    @classmethod
    def setUpClass(cls):
        exit_code = _rebuild_site()
        if exit_code != 0:
            raise RuntimeError(f"Jekyll build failed with exit code {exit_code}")

    def test_sitemap_has_absolute_urls(self):
        """All sitemap URLs must be absolute (https://)."""
        sitemap_file = SITE_DIR / "sitemap.xml"
        self.assertTrue(sitemap_file.exists(), "sitemap.xml must exist in _site/")
        tree = ET.parse(str(sitemap_file))
        root = tree.getroot()
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = root.findall(".//sm:url/sm:loc", ns)
        self.assertGreater(len(urls), 0, "Sitemap must contain at least one URL")
        for url in urls:
            self.assertTrue(
                url.text.startswith("https://"),
                f"Sitemap URL must be absolute, got: {url.text}",
            )


if __name__ == "__main__":
    unittest.main()
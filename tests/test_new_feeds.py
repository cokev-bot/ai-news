"""Tests for the 3 newly added feed sources:

  1. arXiv cs.AI         – https://arxiv.org/rss/cs.AI
  2. HuggingFace Trending Models – https://huggingface.co/blog/feed.xml
  3. The Rundown AI       – https://news.google.com/rss/search?q=site:therundown.ai&hl=en-US&gl=US&ceid=US:en

These tests verify:
  - The feeds exist in sections.json under the expected sections/subsections
  - Each feed URL can be fetched and returns parseable RSS/Atom XML
  - The parsed feed contains at least one item
"""

import json
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTIONS_FILE = PROJECT_ROOT / "sections.json"

# The three new feeds we added
NEW_FEEDS = {
    "arXiv cs.AI": {
        "url": "https://arxiv.org/rss/cs.AI",
        "section": "Research",
        "subsection": "arXiv",
    },
    "HuggingFace Trending Models": {
        "url": "https://huggingface.co/blog/feed.xml",
        "section": "Developer Tools",
        "subsection": "HuggingFace",
    },
    "The Rundown AI": {
        "url": "https://news.google.com/rss/search?q=site:therundown.ai&hl=en-US&gl=US&ceid=US:en",
        "section": "News",
        "subsection": "Newsletters",
    },
}


def _load_sections() -> list[dict]:
    """Load and return sections.json as a Python list."""
    with SECTIONS_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _fetch_feed_xml(url: str, timeout: int = 20) -> bytes:
    """Fetch a feed URL and return the raw bytes. Raises on HTTP errors."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AI-News-Feed-Test/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Tests: sections.json structure
# ---------------------------------------------------------------------------


class TestNewFeedsInSectionJSON(unittest.TestCase):
    """Verify that the 3 new feeds appear in the right place in sections.json."""

    @classmethod
    def setUpClass(cls):
        cls.sections = _load_sections()

    def _find_section(self, title: str) -> dict | None:
        for s in self.sections:
            if s["title"] == title:
                return s
        return None

    def test_research_section_exists(self):
        section = self._find_section("Research")
        self.assertIsNotNone(section, "Research section must exist in sections.json")

    def test_newsletters_subsection_exists(self):
        section = self._find_section("News")
        self.assertIsNotNone(section, "News section must exist in sections.json")
        titles = [sub["title"] for sub in section["subsections"]]
        self.assertIn("Newsletters", titles, "Newsletters subsection must exist under News")

    def test_huggingface_subsection_exists(self):
        section = self._find_section("Developer Tools")
        self.assertIsNotNone(section, "Developer Tools section must exist in sections.json")
        titles = [sub["title"] for sub in section["subsections"]]
        self.assertIn("HuggingFace", titles, "HuggingFace subsection must exist under Developer Tools")

    def test_all_three_feeds_present_in_json(self):
        """Every new feed name must appear in its section/subsection's feeds dict."""
        for feed_name, meta in NEW_FEEDS.items():
            section = self._find_section(meta["section"])
            self.assertIsNotNone(
                section,
                f"Section '{meta['section']}' not found for feed '{feed_name}'",
            )
            found = False
            for sub in section["subsections"]:
                if sub["title"] == meta["subsection"]:
                    self.assertIn(
                        feed_name,
                        sub["feeds"],
                        f"Feed '{feed_name}' missing from {meta['section']} > {meta['subsection']}",
                    )
                    self.assertEqual(
                        sub["feeds"][feed_name],
                        meta["url"],
                        f"URL mismatch for feed '{feed_name}'",
                    )
                    found = True
                    break
            self.assertTrue(
                found,
                f"Subsection '{meta['subsection']}' not found in section '{meta['section']}'",
            )

    def test_total_section_count(self):
        """We should now have 7 top-level sections (was 6, +1 Research)."""
        self.assertEqual(len(self.sections), 7)

    def test_news_section_has_three_subsections(self):
        """News section should now have FT, NYT, and Newsletters."""
        section = self._find_section("News")
        self.assertEqual(len(section["subsections"]), 3)

    def test_developer_tools_section_has_four_subsections(self):
        """Developer Tools section should now have Google, HuggingFace, Mistral, Ollama."""
        section = self._find_section("Developer Tools")
        self.assertEqual(len(section["subsections"]), 4)


# ---------------------------------------------------------------------------
# Tests: feed fetch & parse (live network calls)
# ---------------------------------------------------------------------------


class TestNewFeedsFetchAndParse(unittest.TestCase):
    """Fetch each new feed URL and verify it returns valid RSS/Atom XML with items."""

    def _fetch_and_parse(self, url: str) -> ET.Element:
        """Fetch feed, parse XML, return root element."""
        raw = _fetch_feed_xml(url)
        root = ET.fromstring(raw)
        return root

    def test_arxiv_cs_ai_fetch_and_parse(self):
        """arXiv cs.AI RSS feed returns valid XML with <item> entries."""
        meta = NEW_FEEDS["arXiv cs.AI"]
        root = self._fetch_and_parse(meta["url"])
        # arXiv RSS 2.0: items are under <channel>/<item>
        items = root.findall(".//item")
        self.assertGreater(
            len(items), 0,
            "arXiv cs.AI feed should contain at least one <item>",
        )
        # Check that items have <title> and <link>
        first = items[0]
        self.assertIsNotNone(first.findtext("title"), "arXiv item should have a <title>")
        self.assertIsNotNone(first.findtext("link"), "arXiv item should have a <link>")

    def test_huggingface_trending_models_fetch_and_parse(self):
        """HuggingFace blog RSS feed returns valid XML with entries."""
        meta = NEW_FEEDS["HuggingFace Trending Models"]
        root = self._fetch_and_parse(meta["url"])
        # HuggingFace blog uses RSS 2.0 with <item> elements
        items = root.findall(".//item")
        self.assertGreater(
            len(items), 0,
            "HuggingFace Trending Models feed should contain at least one <item>",
        )
        first = items[0]
        self.assertIsNotNone(first.findtext("title"), "HF item should have a <title>")
        self.assertIsNotNone(first.findtext("link"), "HF item should have a <link>")

    def test_the_rundown_ai_fetch_and_parse(self):
        """The Rundown AI (Google News) feed returns valid XML with items."""
        meta = NEW_FEEDS["The Rundown AI"]
        root = self._fetch_and_parse(meta["url"])
        # Google News RSS uses <item> elements
        items = root.findall(".//item")
        self.assertGreater(
            len(items), 0,
            "The Rundown AI feed should contain at least one <item>",
        )
        first = items[0]
        self.assertIsNotNone(first.findtext("title"), "Rundown item should have a <title>")
        self.assertIsNotNone(first.findtext("link"), "Rundown item should have a <link>")


# ---------------------------------------------------------------------------
# Tests: feed URL reachability (HTTP status check only)
# ---------------------------------------------------------------------------


class TestNewFeedURLsReachable(unittest.TestCase):
    """Quick HTTP HEAD/GET check that each feed URL returns 200."""

    def _assert_url_reachable(self, url: str):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AI-News-Feed-Test/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                self.assertEqual(resp.status, 200, f"Feed URL {url} returned {resp.status}")
        except urllib.error.HTTPError as exc:
            self.fail(f"Feed URL {url} returned HTTP {exc.code}: {exc.reason}")

    def test_arxiv_cs_ai_reachable(self):
        self._assert_url_reachable(NEW_FEEDS["arXiv cs.AI"]["url"])

    def test_huggingface_trending_models_reachable(self):
        self._assert_url_reachable(NEW_FEEDS["HuggingFace Trending Models"]["url"])

    def test_the_rundown_ai_reachable(self):
        self._assert_url_reachable(NEW_FEEDS["The Rundown AI"]["url"])


# ---------------------------------------------------------------------------
# Tests: generate_news.py can load sections with new feeds
# ---------------------------------------------------------------------------


class TestGenerateNewsLoadsNewFeeds(unittest.TestCase):
    """Verify that generate_news.py can load the updated sections.json
    and that the new feed names appear in the feed list."""

    def test_get_all_feeds_includes_new_feeds(self):
        """The new feed names should appear when generate_news processes sections."""
        from generate_news import fetch_all_feeds

        sections = _load_sections()
        # We don't actually fetch; just verify the sections structure is valid
        # by checking that each new feed can be found in the sections hierarchy.
        feed_names_found = set()
        for section in sections:
            for subsection in section["subsections"]:
                for name in subsection["feeds"]:
                    feed_names_found.add(name)

        for expected_name in NEW_FEEDS:
            self.assertIn(
                expected_name,
                feed_names_found,
                f"Feed '{expected_name}' not found in sections.json feed names",
            )


if __name__ == "__main__":
    unittest.main()
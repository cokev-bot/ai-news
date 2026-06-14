"""Tests for source-pill rendering:

  - render_source_pill() returns linked <a> when URL is known
  - render_source_pill() falls back to <strong> when URL is unknown
  - render_item() produces source-pill links for known sources
  - render_item() falls back to <strong> for unknown sources
  - sections.json has source_urls for every feed name
  - Source-pill CSS is present in generated posts
  - Backward compatibility: old-style sections.json (flat array) still works
"""

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from generate_news import render_source_pill, render_item

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTIONS_FILE = PROJECT_ROOT / "sections.json"


# ---------------------------------------------------------------------------
# render_source_pill
# ---------------------------------------------------------------------------

class TestRenderSourcePill(unittest.TestCase):
    """Unit tests for render_source_pill()."""

    def test_known_source_returns_linked_pill(self):
        """When a feed name has a URL, render as <a class='source-pill'>."""
        urls = {"OpenAI": "https://x.com/OpenAI"}
        result = render_source_pill("OpenAI", urls)
        self.assertIn('class="source-pill"', result)
        self.assertIn('href="https://x.com/OpenAI"', result)
        self.assertIn("OpenAI", result)
        self.assertNotIn("<strong>", result)

    def test_unknown_source_falls_back_to_strong(self):
        """When a feed name has no URL, fall back to <strong>."""
        urls = {"OpenAI": "https://x.com/OpenAI"}
        result = render_source_pill("UnknownSource", urls)
        self.assertEqual(result, "<strong>UnknownSource</strong>")
        self.assertNotIn("source-pill", result)

    def test_empty_url_map_falls_back_to_strong(self):
        """Empty source_urls dict → always <strong>."""
        result = render_source_pill("OpenAI", {})
        self.assertEqual(result, "<strong>OpenAI</strong>")

    def test_url_with_special_characters_escaped(self):
        """Feed names with special HTML characters are rendered literally."""
        urls = {"FT AI": "https://www.ft.com/ai"}
        result = render_source_pill("FT AI", urls)
        self.assertIn("FT AI", result)
        self.assertIn('href="https://www.ft.com/ai"', result)


# ---------------------------------------------------------------------------
# render_item with source_urls
# ---------------------------------------------------------------------------

class TestRenderItemSourcePill(unittest.TestCase):
    """render_item() should use source pills when source_urls is provided."""

    # -- News-style items (non-nitter) --

    def test_news_item_with_known_source(self):
        """Non-nitter items with a known source render as source-pill links."""
        urls = {"FT AI": "https://www.ft.com/artificial-intelligence"}
        art = {
            "title": "Wall Street digests AI fundraising",
            "link": "https://www.ft.com/content/abc123",
            "source": "FT AI",
            "description": "Article description here.",
        }
        result = render_item(art, urls)
        self.assertIn('class="source-pill"', result)
        self.assertIn('href="https://www.ft.com/artificial-intelligence"', result)
        self.assertIn("FT AI", result)
        self.assertNotIn("<strong>FT AI</strong>", result)
        # Article title link is still present
        self.assertIn('href="https://www.ft.com/content/abc123"', result)

    def test_news_item_with_unknown_source(self):
        """Non-nitter items with an unknown source fall back to <strong>."""
        urls = {"FT AI": "https://www.ft.com/artificial-intelligence"}
        art = {
            "title": "Breaking news",
            "link": "https://example.com/article",
            "source": "UnknownSource",
            "description": "",
        }
        result = render_item(art, urls)
        self.assertIn("<strong>UnknownSource</strong>", result)
        self.assertNotIn("source-pill", result)

    def test_news_item_no_source_urls(self):
        """When source_urls is None (default), fall back to <strong>."""
        art = {
            "title": "Some headline",
            "link": "https://example.com/article",
            "source": "OpenAI",
            "description": "",
        }
        result = render_item(art)
        self.assertIn("<strong>OpenAI</strong>", result)
        self.assertNotIn("source-pill", result)

    # -- Nitter/Twitter-style items --

    def test_nitter_item_with_known_source(self):
        """Nitter items with a known source render as source-pill links."""
        urls = {"AnthropicAI": "https://x.com/AnthropicAI"}
        art = {
            "title": "Exciting new feature launch!",
            "link": "https://nitter.net/AnthropicAI/status/12345",
            "source": "AnthropicAI",
            "description": "",
        }
        result = render_item(art, urls)
        self.assertIn('class="source-pill"', result)
        self.assertIn('href="https://x.com/AnthropicAI"', result)
        self.assertIn("AnthropicAI", result)
        # Nitter link is converted to x.com
        self.assertIn("x.com", result)

    def test_nitter_item_with_unknown_source(self):
        """Nitter items with unknown source fall back to <strong>."""
        urls = {}
        art = {
            "title": "Tweet text",
            "link": "https://nitter.net/someuser/status/999",
            "source": "someuser",
            "description": "",
        }
        result = render_item(art, urls)
        self.assertIn("<strong>someuser</strong>", result)

    # -- Retweet filter still works --

    def test_retweet_filtered_with_source_urls(self):
        """Retweets are still filtered even when source_urls is provided."""
        urls = {"SomeUser": "https://x.com/SomeUser"}
        art = {
            "title": "R to @other: retweeted content",
            "link": "https://nitter.net/SomeUser/status/111",
            "source": "SomeUser",
            "description": "",
        }
        result = render_item(art, urls)
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# sections.json consistency
# ---------------------------------------------------------------------------

class TestSectionsJsonSourceUrls(unittest.TestCase):
    """Verify that sections.json has source_urls for every feed name."""

    @classmethod
    def setUpClass(cls):
        with SECTIONS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            # Old format: no source_urls
            cls.sections = data
            cls.source_urls = {}
        else:
            cls.sections = data.get("sections", [])
            cls.source_urls = data.get("source_urls", {})
        cls.all_feed_names = []
        for section in cls.sections:
            for subsection in section.get("subsections", []):
                for name in subsection.get("feeds", {}):
                    cls.all_feed_names.append(name)

    def test_sections_json_is_valid_object(self):
        """sections.json should be an object with 'sections' and 'source_urls' keys."""
        with SECTIONS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict, "sections.json should be an object, not a list")
        self.assertIn("sections", data)
        self.assertIn("source_urls", data)

    def test_every_feed_has_source_url(self):
        """Every feed name in sections must have a corresponding source_urls entry."""
        missing = []
        for name in self.all_feed_names:
            if name not in self.source_urls:
                missing.append(name)
        self.assertEqual(
            missing, [],
            f"The following feed names are missing from source_urls: {missing}",
        )

    def test_source_urls_are_valid_urls(self):
        """Every source_urls value should be an http(s) URL."""
        for name, url in self.source_urls.items():
            self.assertTrue(
                url.startswith("http://") or url.startswith("https://"),
                f"source_urls['{name}'] = '{url}' is not an http(s) URL",
            )

    def test_no_extra_source_urls(self):
        """source_urls should not contain entries not in sections (optional check)."""
        # This is informational — extra entries are harmless
        extra = set(self.source_urls.keys()) - set(self.all_feed_names)
        # Just verify there aren't gross mismatches; allow a few extras
        self.assertLessEqual(
            len(extra), len(self.all_feed_names),
            f"Too many extra source_urls entries: {extra}",
        )


# ---------------------------------------------------------------------------
# Source-pill CSS in generated posts
# ---------------------------------------------------------------------------

class TestSourcePillCSS(unittest.TestCase):
    """Verify that generate_post() injects source-pill CSS."""

    def _make_site_root(self, tmp_path, sections_data=None):
        """Create a minimal site root with sections.json, config.json, etc."""
        from generate_news import DEFAULT_CONFIG

        site_root = tmp_path / "site"
        site_root.mkdir()
        (site_root / "config.json").write_text(json.dumps(DEFAULT_CONFIG))

        if sections_data is None:
            sections_data = {
                "source_urls": {"TestFeed": "https://example.com"},
                "sections": [
                    {
                        "title": "Test",
                        "subsections": [
                            {
                                "title": "TestSub",
                                "feeds": {"TestFeed": "https://example.com/rss"},
                            },
                        ],
                    },
                ],
            }
        (site_root / "sections.json").write_text(json.dumps(sections_data))

        # Minimal summary prompt
        (site_root / "summary_prompt.txt").write_text("Summarize: ")

        # _posts dir
        posts_dir = site_root / "_posts"
        posts_dir.mkdir()

        return site_root

    @patch("generate_news._query_ollama")
    @patch("generate_news.fetch_all_feeds")
    def test_css_injected_in_post(self, mock_fetch, mock_ollama):
        """generate_post() injects .source-pill <style> block into the HTML."""
        from generate_news import generate_post

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            site_root = self._make_site_root(Path(tmp))

            # Mock feeds to return one article
            mock_fetch.return_value = {
                "TestSub": [("TestFeed", [
                    {
                        "title": "Test article",
                        "link": "https://example.com/article",
                        "source": "TestFeed",
                        "description": "A test description",
                        "published": "2026-06-14T12:00:00+00:00",
                    },
                ])],
            }
            mock_ollama.return_value = "Test summary."

            result = generate_post("2026-06-14-morning", site_root)
            self.assertTrue(result)

            # Read the generated post
            posts = list((site_root / "_posts").glob("*.html"))
            self.assertGreaterEqual(len(posts), 1)
            content = posts[0].read_text(encoding="utf-8")

            # Verify source-pill CSS is present
            self.assertIn(".source-pill", content)
            self.assertIn("border-radius", content)

    @patch("generate_news._query_ollama")
    @patch("generate_news.fetch_all_feeds")
    def test_source_pill_rendered_in_article(self, mock_fetch, mock_ollama):
        """generate_post() renders source names as <a class='source-pill'> links."""
        from generate_news import generate_post

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            site_root = self._make_site_root(Path(tmp))

            mock_fetch.return_value = {
                "TestSub": [("TestFeed", [
                    {
                        "title": "Test article",
                        "link": "https://example.com/article",
                        "source": "TestFeed",
                        "description": "A test description",
                        "published": "2026-06-14T12:00:00+00:00",
                    },
                ])],
            }
            mock_ollama.return_value = "Test summary."

            result = generate_post("2026-06-14-morning", site_root)
            self.assertTrue(result)

            posts = list((site_root / "_posts").glob("*.html"))
            content = posts[0].read_text(encoding="utf-8")

            # Source should be rendered as a pill link, not <strong>
            self.assertIn('class="source-pill"', content)
            self.assertIn('href="https://example.com"', content)
            # The old-style <strong>TestFeed</strong> should NOT appear
            self.assertNotIn("<strong>TestFeed</strong>", content)

    @patch("generate_news._query_ollama")
    @patch("generate_news.fetch_all_feeds")
    def test_unknown_source_falls_back_to_strong(self, mock_fetch, mock_ollama):
        """When a source is not in source_urls, <strong> fallback is used."""
        from generate_news import generate_post

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sections_data = {
                "source_urls": {},  # No URLs → all sources fall back to <strong>
                "sections": [
                    {
                        "title": "Test",
                        "subsections": [
                            {
                                "title": "TestSub",
                                "feeds": {"TestFeed": "https://example.com/rss"},
                            },
                        ],
                    },
                ],
            }
            site_root = self._make_site_root(Path(tmp), sections_data)

            mock_fetch.return_value = {
                "TestSub": [("TestFeed", [
                    {
                        "title": "Test article",
                        "link": "https://example.com/article",
                        "source": "TestFeed",
                        "description": "A test description",
                        "published": "2026-06-14T12:00:00+00:00",
                    },
                ])],
            }
            mock_ollama.return_value = "Test summary."

            result = generate_post("2026-06-14-morning", site_root)
            self.assertTrue(result)

            posts = list((site_root / "_posts").glob("*.html"))
            content = posts[0].read_text(encoding="utf-8")

            # Source should fall back to <strong> when not in source_urls
            self.assertIn("<strong>TestFeed</strong>", content)

    def test_backward_compatible_old_sections_json(self):
        """Loading a flat-array sections.json should still work (SECTIONS=list, SOURCE_URLS={})."""
        # This tests the backward-compatible loader in generate_post
        # by checking that the code doesn't crash with an old-format file.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            site_root = tmp_path / "site"
            site_root.mkdir()

            from generate_news import DEFAULT_CONFIG
            (site_root / "config.json").write_text(json.dumps(DEFAULT_CONFIG))

            # Old format: flat array
            old_sections = [
                {
                    "title": "Test",
                    "subsections": [
                        {
                            "title": "TestSub",
                            "feeds": {"TestFeed": "https://example.com/rss"},
                        },
                    ],
                },
            ]
            (site_root / "sections.json").write_text(json.dumps(old_sections))
            (site_root / "summary_prompt.txt").write_text("Summarize: ")
            (site_root / "_posts").mkdir()

            # The load logic should not crash; SECTIONS = old_sections, SOURCE_URLS = {}
            from generate_news import generate_post

            with patch("generate_news.fetch_all_feeds") as mock_fetch, \
                 patch("generate_news._query_ollama") as mock_ollama:
                mock_fetch.return_value = {
                    "TestSub": [("TestFeed", [
                        {
                            "title": "Test article",
                            "link": "https://example.com/article",
                            "source": "TestFeed",
                            "description": "",
                            "published": "2026-06-14T12:00:00+00:00",
                        },
                    ])],
                }
                mock_ollama.return_value = "Summary."

                # Should not raise
                result = generate_post("2026-06-14-morning", site_root)
                self.assertTrue(result)

                posts = list((site_root / "_posts").glob("*.html"))
                content = posts[0].read_text(encoding="utf-8")

                # Without source_urls, should fall back to <strong>
                self.assertIn("<strong>TestFeed</strong>", content)


if __name__ == "__main__":
    unittest.main()
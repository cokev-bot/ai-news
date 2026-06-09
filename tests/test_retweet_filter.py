"""Tests for is_retweet() and its integration with fetch_feed / render_item."""

import unittest
from generate_news import is_retweet, render_item, fetch_feed
from unittest.mock import patch, MagicMock
import xml.etree.ElementTree as ET


class TestIsRetweet(unittest.TestCase):
    """Unit tests for the is_retweet() function."""

    def test_retweet_prefix_detected(self):
        """Titles starting with 'R to @' are retweets."""
        self.assertTrue(is_retweet("R to @antigravity: CLI Walkthrough"))
        self.assertTrue(is_retweet("R to @user: some text"))
        self.assertTrue(is_retweet("R to @username:"))

    def test_normal_titles_not_retweet(self):
        """Normal titles should not be flagged as retweets."""
        self.assertFalse(is_retweet("New GPT-5 Release"))
        self.assertFalse(is_retweet("R to someone without @"))
        self.assertFalse(is_retweet("RT @user: something"))
        self.assertFalse(is_retweet(""))

    def test_exact_prefix_match(self):
        """Only 'R to @' at the start counts."""
        self.assertFalse(is_retweet("Something R to @user: text"))
        self.assertFalse(is_retweet("Breaking: R to @user: text"))

    def test_whitespace_preceding(self):
        """Leading whitespace means it's NOT a retweet (doesn't start with 'R to @')."""
        self.assertFalse(is_retweet(" R to @user: text"))


class TestRenderItemRetweet(unittest.TestCase):
    """render_item() should return empty string for retweet items."""

    def test_render_retweet_returns_empty(self):
        art = {
            "title": "R to @antigravity: CLI Walkthrough",
            "link": "https://nitter.net/user/status/123",
            "source": "AI Labs",
            "description": "",
        }
        self.assertEqual(render_item(art), "")

    def test_render_normal_nitter_item(self):
        art = {
            "title": "Great new model released!",
            "link": "https://nitter.net/user/status/456",
            "source": "AI Labs",
            "description": "",
        }
        result = render_item(art)
        self.assertNotEqual(result, "")
        self.assertIn("Great new model released!", result)

    def test_render_normal_news_item(self):
        art = {
            "title": "OpenAI announces GPT-5",
            "link": "https://example.com/article",
            "source": "TechCrunch",
            "description": "Full article text here.",
        }
        result = render_item(art)
        self.assertNotEqual(result, "")
        self.assertIn("OpenAI announces GPT-5", result)


class TestFetchFeedRetweetFilter(unittest.TestCase):
    """fetch_feed() should filter out retweet items before they enter articles."""

    def _make_rss(self, items):
        """Build a minimal RSS XML string from a list of (title, link, pubDate) tuples."""
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0"><channel><title>Test</title>',
        ]
        for title, link, pub_date in items:
            parts.append(f"<item><title>{title}</title><link>{link}</link>")
            if pub_date:
                parts.append(f"<pubDate>{pub_date}</pubDate>")
            parts.append("<description>desc</description></item>")
        parts.append("</channel></rss>")
        return "".join(parts).encode("utf-8")

    @patch("generate_news._http_get_with_retry")
    def test_fetch_feed_filters_retweets(self, mock_get):
        """Retweet items should be dropped from fetch_feed results."""
        rss = self._make_rss([
            ("R to @antigravity: CLI Walkthrough", "https://nitter.net/antigravity/status/1", None),
            ("Real breaking news", "https://example.com/real", None),
            ("R to @user: another retweet", "https://nitter.net/user/status/2", None),
        ])
        mock_get.return_value = rss
        articles = fetch_feed("TestFeed", "https://nitter.net/feed")
        # Only the non-retweet article should survive
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "Real breaking news")

    @patch("generate_news._http_get_with_retry")
    def test_fetch_feed_no_retweets_pass_through(self, mock_get):
        """All non-retweet items should pass through normally."""
        rss = self._make_rss([
            ("Title 1", "https://example.com/1", None),
            ("Title 2", "https://example.com/2", None),
        ])
        mock_get.return_value = rss
        articles = fetch_feed("TestFeed", "https://example.com/feed")
        self.assertEqual(len(articles), 2)

    @patch("generate_news._http_get_with_retry")
    def test_fetch_feed_all_retweets_returns_empty(self, mock_get):
        """If all items are retweets, fetch_feed returns an empty list."""
        rss = self._make_rss([
            ("R to @a: text", "https://nitter.net/a/status/1", None),
            ("R to @b: text", "https://nitter.net/b/status/2", None),
        ])
        mock_get.return_value = rss
        articles = fetch_feed("TestFeed", "https://nitter.net/feed")
        self.assertEqual(len(articles), 0)


if __name__ == "__main__":
    unittest.main()
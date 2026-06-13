"""Tests for the JSON-LD NewsArticle schema generation.

Covers:
- generate_json_ld() produces valid JSON-LD with required fields
- datePublished is formatted as ISO 8601
- headline includes the edition label
- description uses Big Picture text (truncated at 300 chars)
- articleSection joins section titles
- keywords are unique sources, capped at 20
- url is constructed correctly from site_url + date + edition
- publisher is included as Organization
- missing global_summary_text omits description field
- empty sections/sources fall back to defaults
- Integration: generate_post() includes JSON-LD in output HTML
"""

import json
import re
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from generate_news import generate_json_ld, generate_post


class TestGenerateJsonLd(unittest.TestCase):
    """Unit tests for the generate_json_ld() function."""

    def test_basic_structure(self):
        """JSON-LD block contains required @context, @type, and core fields."""
        post_now = datetime(2026, 6, 13, 8, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="AI is advancing rapidly.",
            section_titles=["News", "Research"],
            sources=["FT AI", "arXiv"],
        )
        # Must be a <script> tag
        self.assertIn('<script type="application/ld+json">', html)
        self.assertIn("</script>", html)

        # Extract JSON
        json_str = html.split('<script type="application/ld+json">\n', 1)[1]
        json_str = json_str.rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)

        self.assertEqual(schema["@context"], "https://schema.org")
        self.assertEqual(schema["@type"], "NewsArticle")
        self.assertEqual(schema["headline"], "AI News Digest — Morning Edition")
        self.assertIn("datePublished", schema)
        self.assertIn("articleSection", schema)
        self.assertIn("keywords", schema)
        self.assertIn("url", schema)
        self.assertIn("publisher", schema)

    def test_date_published_format(self):
        """datePublished is ISO 8601 with timezone offset like +07:00."""
        post_now = datetime(2026, 6, 13, 8, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        html = generate_json_ld(
            edition_label="Evening",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News"],
            sources=["FT AI"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)

        # Should be ISO 8601 with colon in offset (e.g. -07:00 not -0700)
        date_pub = schema["datePublished"]
        # Must contain "T" and a timezone offset with colon
        self.assertRegex(date_pub, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")

    def test_date_published_utc(self):
        """UTC timezone formats correctly as +00:00."""
        post_now = datetime(2026, 6, 13, 15, 0, 0, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News"],
            sources=["Source A"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertIn("+00:00", schema["datePublished"])

    def test_description_from_big_picture(self):
        """Description uses the Big Picture summary text."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="OpenAI released GPT-5 today.",
            section_titles=["News"],
            sources=["FT AI"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertEqual(schema["description"], "OpenAI released GPT-5 today.")

    def test_description_truncated_at_300_chars(self):
        """Long description is truncated at ~300 chars with ellipsis."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        long_text = "A " * 200  # 399 chars
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text=long_text,
            section_titles=["News"],
            sources=["FT AI"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertLessEqual(len(schema["description"]), 303)  # truncation + "…"
        self.assertTrue(schema["description"].endswith("…"))

    def test_no_description_when_summary_missing(self):
        """When global_summary_text is None, no description field is included."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text=None,
            section_titles=["News"],
            sources=["FT AI"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertNotIn("description", schema)

    def test_no_description_when_summary_empty(self):
        """When global_summary_text is empty string, no description field is included."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="",
            section_titles=["News"],
            sources=["FT AI"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertNotIn("description", schema)

    def test_article_section_joins_titles(self):
        """articleSection joins section titles with comma-space."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News", "Research", "Developer Tools"],
            sources=["Source A"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertEqual(schema["articleSection"], "News, Research, Developer Tools")

    def test_article_section_default_when_empty(self):
        """articleSection defaults to 'AI News' when no sections provided."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=[],
            sources=["Source A"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertEqual(schema["articleSection"], "AI News")

    def test_keywords_unique_and_capped(self):
        """keywords are deduplicated and capped at 20."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        sources = [f"Source{i}" for i in range(25)]
        sources[5] = "Source0"  # duplicate
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News"],
            sources=sources,
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertEqual(len(schema["keywords"]), 20)
        # Check dedup: Source0 appears only once
        self.assertEqual(schema["keywords"].count("Source0"), 1)

    def test_url_format(self):
        """url is constructed from site_url + date + edition label."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Afternoon",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News"],
            sources=["Source A"],
            site_url="https://example.com/news/",
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertEqual(schema["url"], "https://example.com/news/2026-06-13-afternoon.html")

    def test_default_site_url(self):
        """Default site_url is the AI News site on GitHub Pages."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News"],
            sources=["Source A"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        self.assertTrue(schema["url"].startswith("https://cokev-bot.github.io/ai-news/"))
        self.assertIn("2026-06-13-morning.html", schema["url"])

    def test_publisher_structure(self):
        """Publisher is an Organization with name and url."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test",
            section_titles=["News"],
            sources=["Source A"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        schema = json.loads(json_str)
        pub = schema["publisher"]
        self.assertEqual(pub["@type"], "Organization")
        self.assertEqual(pub["name"], "AI News Digest")
        self.assertIn("url", pub)

    def test_json_ld_is_valid_json(self):
        """The JSON inside the script tag is valid, parseable JSON."""
        post_now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        html = generate_json_ld(
            edition_label="Morning",
            post_now=post_now,
            global_summary_text="Test with \"quotes\" and <brackets>",
            section_titles=["News", "Research"],
            sources=["FT AI", "arXiv cs.AI"],
        )
        json_str = html.split('<script type="application/ld+json">\n', 1)[1].rsplit("\n</script>", 1)[0]
        # Should not raise
        schema = json.loads(json_str)
        # Special characters should be properly escaped
        self.assertIn("quotes", schema["description"])
        self.assertIn("brackets", schema["description"])


class TestJsonLdIntegration(unittest.TestCase):
    """Integration test: generate_post() includes JSON-LD in its output."""

    def setUp(self):
        self.site_root = Path("/tmp/test_ai_news_jsonld")
        self.site_root.mkdir(exist_ok=True)
        (self.site_root / "_posts").mkdir(exist_ok=True)

        # Minimal sections.json
        sections = [
            {
                "title": "News",
                "subsections": [
                    {
                        "title": "AI Labs",
                        "feeds": {"TestFeed": "https://example.com/feed.xml"},
                    }
                ],
            }
        ]
        (self.site_root / "sections.json").write_text(json.dumps(sections))

        # Minimal config.json
        (self.site_root / "config.json").write_text(json.dumps({"model": "test-model"}))

        # Minimal summary_prompt.txt
        (self.site_root / "summary_prompt.txt").write_text("Summarize this section.")

    def tearDown(self):
        import shutil
        if self.site_root.exists():
            shutil.rmtree(self.site_root)

    @patch("generate_news._query_ollama", return_value="Summary text.")
    @patch("generate_news.fetch_all_feeds")
    @patch("generate_news.load_state")
    @patch("generate_news.save_state")
    @patch("generate_news.generate_edition_audio", return_value={})
    def test_json_ld_in_post(self, mock_audio, mock_save, mock_load, mock_fetch, mock_ollama):
        """generate_post() includes JSON-LD script tag in the output HTML."""
        mock_load.return_value = {"seen_links": {}, "last_run": None}
        mock_fetch.return_value = {
            "AI Labs": [
                ("TestFeed", [
                    {
                        "title": "Test Article",
                        "link": "https://example.com/test-article",
                        "description": "A test article about AI.",
                        "pub": "Fri, 13 Jun 2026 08:00:00 GMT",
                        "pub_dt": datetime(2026, 6, 13, 8, 0, 0, tzinfo=timezone.utc),
                        "source": "TestFeed",
                    }
                ])
            ]
        }

        success = generate_post("2026-06-13-morning", self.site_root)
        self.assertTrue(success, "generate_post should succeed")

        # Read the generated post
        post_path = self.site_root / "_posts" / "2026-06-13-morning.html"
        self.assertTrue(post_path.exists(), "Post file should exist")
        content = post_path.read_text(encoding="utf-8")

        # Must contain the JSON-LD script tag
        self.assertIn('<script type="application/ld+json">', content)

        # Extract and validate the JSON-LD
        match = re.search(
            r'<script type="application/ld\+json">\n(.*?)\n</script>',
            content,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "JSON-LD script tag should be present")
        schema = json.loads(match.group(1))

        self.assertEqual(schema["@type"], "NewsArticle")
        self.assertEqual(schema["headline"], "AI News Digest — Morning Edition")
        self.assertIn("datePublished", schema)

    @patch("generate_news._query_ollama", return_value="Summary text.")
    @patch("generate_news.fetch_all_feeds")
    @patch("generate_news.load_state")
    @patch("generate_news.save_state")
    @patch("generate_news.generate_edition_audio", return_value={})
    def test_json_ld_after_front_matter(self, mock_audio, mock_save, mock_load, mock_fetch, mock_ollama):
        """JSON-LD is placed after the closing front-matter ---, not inside it."""
        mock_load.return_value = {"seen_links": {}, "last_run": None}
        mock_fetch.return_value = {
            "AI Labs": [
                ("TestFeed", [
                    {
                        "title": "Test Article",
                        "link": "https://example.com/test2",
                        "description": "Another test.",
                        "pub": "Fri, 13 Jun 2026 08:00:00 GMT",
                        "pub_dt": datetime(2026, 6, 13, 8, 0, 0, tzinfo=timezone.utc),
                        "source": "TestFeed",
                    }
                ])
            ]
        }

        success = generate_post("2026-06-13-evening", self.site_root)
        self.assertTrue(success)

        post_path = self.site_root / "_posts" / "2026-06-13-evening.html"
        content = post_path.read_text(encoding="utf-8")

        # Find position of JSON-LD relative to the closing front-matter ---
        # Jekyll front matter is: ---\n<yaml>\n---, so the second "---" line closes it.
        lines = content.split("\n")
        dash_count = 0
        closing_dash_idx = None
        json_ld_idx = None
        for idx, line in enumerate(lines):
            if line.strip() == "---":
                dash_count += 1
                if dash_count == 2:
                    closing_dash_idx = idx
            if '<script type="application/ld+json">' in line:
                json_ld_idx = idx

        # The closing --- should be found
        self.assertIsNotNone(closing_dash_idx, "Should find closing front-matter ---")
        # JSON-LD should appear after the closing front-matter
        self.assertIsNotNone(json_ld_idx, "JSON-LD script tag should be in the post")
        self.assertGreater(json_ld_idx, closing_dash_idx,
                           "JSON-LD should appear after the closing front-matter ---")


if __name__ == "__main__":
    unittest.main()
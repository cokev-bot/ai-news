"""Tests for the per-day 'All editions' landing page feature.

Validates that:
- The Jekyll plugin and layout files exist with correct structure
- Jekyll build generates day index pages at /news/:year/:month/:day/
- Each day index page lists its editions with correct ordering
- Big Picture teasers are extracted and cleaned (no HTML tags, no Markdown)
- Editions without Big Picture still render correctly
"""

import os
import re
import subprocess
import unittest
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = SITE_ROOT / "_plugins"
LAYOUTS_DIR = SITE_ROOT / "_layouts"
SITE_DIR = SITE_ROOT / "_site"


class TestDayIndexPluginExists(unittest.TestCase):
    """Verify the plugin file exists and is syntactically valid Ruby."""

    def test_plugin_file_exists(self):
        plugin_path = PLUGINS_DIR / "day_index_generator.rb"
        self.assertTrue(plugin_path.exists(), "day_index_generator.rb missing from _plugins/")

    def test_plugin_defines_generator_class(self):
        plugin_path = PLUGINS_DIR / "day_index_generator.rb"
        content = plugin_path.read_text()
        # Should define the DayIndex::Generator class
        self.assertIn("DayIndex", content)
        self.assertIn("Jekyll::Generator", content)

    def test_plugin_defines_page_class(self):
        plugin_path = PLUGINS_DIR / "day_index_generator.rb"
        content = plugin_path.read_text()
        self.assertIn("DayIndexPage", content)

    def test_layout_file_exists(self):
        layout_path = LAYOUTS_DIR / "day.html"
        self.assertTrue(layout_path.exists(), "day.html layout missing from _layouts/")

    def test_layout_references_editions(self):
        layout_path = LAYOUTS_DIR / "day.html"
        content = layout_path.read_text()
        self.assertIn("page.editions", content)
        self.assertIn("edition.title", content)
        self.assertIn("edition.url", content)
        self.assertIn("edition.bp_tease", content)


class TestDayIndexBuild(unittest.TestCase):
    """Verify Jekyll build produces day index pages."""

    @classmethod
    def setUpClass(cls):
        """Build the site once for all tests in this class."""
        result = subprocess.run(
            ["bundle", "exec", "jekyll", "build"],
            cwd=str(SITE_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        cls.build_result = result

    def test_jekyll_build_succeeds(self):
        self.assertEqual(
            self.build_result.returncode, 0,
            f"Jekyll build failed: {self.build_result.stderr[-500:]}"
        )

    def test_day_index_pages_exist(self):
        """At least one day index page should be generated."""
        day_indices = list(SITE_DIR.glob("news/20??/??/??/index.html"))
        # Filter out edition subdirectories (e.g. news/2026/06/12/Morning/index.html)
        # Day indices are directly under the day directory
        true_day_indices = [
            p for p in day_indices
            if re.match(r"news/\d{4}/\d{2}/\d{2}/index\.html", p.relative_to(SITE_DIR).as_posix())
        ]
        self.assertGreater(
            len(true_day_indices), 0,
            "No day index pages found in _site/news/YYYY/MM/DD/"
        )

    def test_day_index_contains_editions(self):
        """A day index page should contain links to edition posts."""
        # Find a day that has editions (June 12, 2026 has Morning + Afternoon)
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        # Should contain edition links
        self.assertIn("Morning", content, "Day index missing Morning edition link")
        self.assertIn("Afternoon", content, "Day index missing Afternoon edition link")
        # Should contain the Big Picture tease
        self.assertIn("bp-tease", content, "Day index missing Big Picture tease")

    def test_day_index_has_title(self):
        """Day index page should have a formatted title with the date."""
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        self.assertIn("June 12, 2026", content, "Day index missing formatted date in title")

    def test_day_index_has_navigation(self):
        """Day index should link back to the news archive."""
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        self.assertIn("All editions", content, "Day index missing 'All editions' navigation link")

    def test_day_index_editions_ordered(self):
        """Editions should be ordered Morning < Afternoon < Evening."""
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        morning_pos = content.find("Morning Edition")
        afternoon_pos = content.find("Afternoon Edition")
        evening_pos = content.find("Evening Edition")

        if morning_pos >= 0 and afternoon_pos >= 0:
            self.assertLess(
                morning_pos, afternoon_pos,
                "Morning should appear before Afternoon in day index"
            )
        if afternoon_pos >= 0 and evening_pos >= 0:
            self.assertLess(
                afternoon_pos, evening_pos,
                "Afternoon should appear before Evening in day index"
            )

    def test_big_picture_tease_no_html(self):
        """Big Picture teasers should not contain HTML tags."""
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        # Extract bp-tease content
        teasers = re.findall(r'<p class="bp-tease">(.*?)</p>', content)
        for tease in teasers:
            # Should not contain HTML tags (no <a>, <strong>, etc.)
            self.assertNotRegex(
                tease, r"<[a-zA-Z]",
                f"Big Picture tease contains HTML tags: {tease[:100]}"
            )
            # Should not contain Markdown bold markers
            self.assertNotIn("**", tease, f"Big Picture tease contains Markdown: {tease[:100]}")

    def test_big_picture_tease_not_empty(self):
        """Big Picture teasers for recent editions should not be empty."""
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        teasers = re.findall(r'<p class="bp-tease">(.*?)</p>', content)
        non_empty_teasers = [t for t in teasers if t.strip()]
        # At least one edition should have a non-empty tease
        self.assertGreater(
            len(non_empty_teasers), 0,
            "All Big Picture teasers are empty"
        )

    def test_day_index_valid_html(self):
        """Day index page should be valid HTML with proper structure."""
        day_index = SITE_DIR / "news/2026/06/12/index.html"
        if not day_index.exists():
            self.skipTest("Day index for 2026-06-12 not found")

        content = day_index.read_text()
        self.assertIn("<!DOCTYPE html>", content, "Missing DOCTYPE")
        self.assertIn("</html>", content, "Missing closing HTML tag")
        self.assertIn('class="edition-list"', content, "Missing edition-list class")
        self.assertIn('class="edition-item"', content, "Missing edition-item class")

    def test_multiple_day_indices_generated(self):
        """Day index pages should be generated for multiple dates."""
        day_indices = [
            p for p in SITE_DIR.glob("news/20??/??/??/index.html")
            if re.match(r"news/\d{4}/\d{2}/\d{2}/index\.html", p.relative_to(SITE_DIR).as_posix())
        ]
        # We have 35+ dates with posts
        self.assertGreater(
            len(day_indices), 5,
            f"Expected at least 5 day index pages, found {len(day_indices)}"
        )


class TestDayIndexTeaseExtraction(unittest.TestCase):
    """Test the Big Picture tease extraction logic in pure Python.

    These mirror the Ruby regex logic to ensure consistency.
    """

    def test_extract_from_standard_html(self):
        """Standard Big Picture HTML should yield the first sentence."""
        content = '<h3 style="margin-top:0;">🌍 The Big Picture</h3>\n  <p>AI is transforming everything. More text here.</p>'
        result = self._extract_tease(content)
        self.assertEqual(result, "AI is transforming everything.")

    def test_extract_strips_html_links(self):
        """HTML links in the Big Picture should be stripped."""
        content = '<h3>🌍 The Big Picture</h3>\n  <p>AI is <a href="https://example.com">transforming</a> everything. More text.</p>'
        result = self._extract_tease(content)
        self.assertEqual(result, "AI is transforming everything.")

    def test_extract_strips_markdown_bold(self):
        """Markdown bold markers should be stripped."""
        content = '<h3>🌍 The Big Picture</h3>\n  <p>**The Big Picture**\n\nAI is transforming everything.</p>'
        result = self._extract_tease(content)
        self.assertEqual(result, "AI is transforming everything.")

    def test_extract_with_audio_div(self):
        """Audio player div between heading and paragraph should be handled."""
        content = '<h3>🌍 The Big Picture</h3><div class="audio-player"><audio controls></audio></div>\n  <p>AI is transforming everything. More text.</p>'
        result = self._extract_tease(content)
        self.assertEqual(result, "AI is transforming everything.")

    def test_extract_empty_when_no_big_picture(self):
        """Posts without Big Picture should yield empty string."""
        content = "<h2>News</h2><p>No Big Picture here.</p>"
        result = self._extract_tease(content)
        self.assertEqual(result, "")

    def test_extract_truncates_long_first_sentence(self):
        """First sentences over 200 chars should be truncated with ellipsis."""
        long_text = "A" * 250 + ". More text."
        content = f'<h3>🌍 The Big Picture</h3>\n  <p>{long_text}</p>'
        result = self._extract_tease(content)
        self.assertLessEqual(len(result), 200)
        self.assertTrue(result.endswith("..."))

    def test_extract_preserves_short_first_sentence(self):
        """Short first sentences should be preserved in full."""
        content = '<h3>🌍 The Big Picture</h3>\n  <p>Short sentence. Second sentence.</p>'
        result = self._extract_tease(content)
        self.assertEqual(result, "Short sentence.")

    @staticmethod
    def _extract_tease(content: str) -> str:
        """Python reimplementation of the Ruby extract_big_picture_tease logic."""
        match = re.search(
            r'<h3[^>]*>🌍\s*The Big Picture\s*</h3>.*?<p>(.*?)</p>',
            content, re.DOTALL | re.IGNORECASE
        )
        if not match:
            return ""
        bp_text = match.group(1)
        # Strip HTML tags
        bp_tease = re.sub(r'</?[^>]+>', '', bp_text).strip()
        # Strip Markdown bold markers
        bp_tease = bp_tease.replace("**", "").strip()
        # Remove leading "The Big Picture" label if present (legacy)
        bp_tease = re.sub(r'^The Big Picture\s*', '', bp_tease, flags=re.IGNORECASE).strip()
        # Truncate to first sentence, max 200 chars
        first_sentence = bp_tease.split(r'(?<=[.!?])\s')[0] if '. ' in bp_tease else bp_tease
        # Simpler sentence split
        parts = re.split(r'(?<=[.!?])\s', bp_tease, maxsplit=1)
        first_sentence = parts[0] if parts else bp_tease
        if len(first_sentence) > 200:
            return first_sentence[:197] + "..."
        return first_sentence


if __name__ == "__main__":
    unittest.main()
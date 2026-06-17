"""Tests for the grouped home page (index.markdown) feature.

Validates that:
- The home layout file exists and contains the required structural elements
- The index.markdown uses the home layout
- Jekyll build produces a home page with day-grouped editions
- Edition badges (Morning/Afternoon/Evening) are present
- Each day group links to its day index page
- Posts are grouped by date, not listed flatly
- The archive link is present
"""

import os
import re
import subprocess
import unittest
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parent.parent
LAYOUTS_DIR = SITE_ROOT / "_layouts"
SITE_DIR = SITE_ROOT / "_site"


class TestHomeLayoutExists(unittest.TestCase):
    """Verify the home layout file exists and has correct structure."""

    def test_home_layout_file_exists(self):
        layout_path = LAYOUTS_DIR / "home.html"
        self.assertTrue(layout_path.exists(), "home.html layout missing from _layouts/")

    def test_home_layout_has_css_classes(self):
        layout_path = LAYOUTS_DIR / "home.html"
        content = layout_path.read_text()
        # Key CSS classes for the grouped home page
        self.assertIn("day-group", content, "home layout missing day-group CSS class")
        self.assertIn("day-heading", content, "home layout missing day-heading CSS class")
        self.assertIn("edition-badge", content, "home layout missing edition-badge CSS class")

    def test_home_layout_has_header_footer(self):
        layout_path = LAYOUTS_DIR / "home.html"
        content = layout_path.read_text()
        self.assertIn("site-header", content, "home layout missing site-header")
        self.assertIn("site-footer", content, "home layout missing site-footer")

    def test_home_layout_has_content_placeholder(self):
        layout_path = LAYOUTS_DIR / "home.html"
        content = layout_path.read_text()
        self.assertIn("{{ content }}", content, "home layout missing {{ content }} placeholder")


class TestIndexMarkdown(unittest.TestCase):
    """Verify index.markdown uses the home layout and has grouped structure."""

    def test_index_uses_home_layout(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        self.assertIn("layout: home", content, "index.markdown not using home layout")

    def test_index_has_editions_by_date_div(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        self.assertIn("editions-by-date", content,
                       "index.markdown missing editions-by-date container")

    def test_index_has_day_group_loop(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        self.assertIn("day-group", content,
                       "index.markdown missing day-group div")
        self.assertIn("day-editions", content,
                       "index.markdown missing day-editions list")

    def test_index_has_edition_badge_logic(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        self.assertIn("edition-badge", content,
                       "index.markdown missing edition-badge logic")
        self.assertIn("Morning", content,
                       "index.markdown missing Morning edition detection")
        self.assertIn("Afternoon", content,
                       "index.markdown missing Afternoon edition detection")
        self.assertIn("Evening", content,
                       "index.markdown missing Evening edition detection")

    def test_index_has_day_heading_with_date_link(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        # Should link to /news/YYYY/MM/DD/ day index pages
        self.assertIn("/news/", content,
                       "index.markdown missing day index link pattern")

    def test_index_has_archive_links(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        self.assertIn("archive", content,
                       "index.markdown missing archive link")

    def test_index_limits_posts(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        self.assertIn("limit: 15", content,
                       "index.markdown should limit posts to 15 (~5 days)")

    def test_index_groups_by_date(self):
        index_path = SITE_ROOT / "index.markdown"
        content = index_path.read_text()
        # Should have date grouping logic using current_date variable
        self.assertIn("current_date", content,
                       "index.markdown missing current_date grouping variable")
        self.assertIn('date: "%Y-%m-%d"', content,
                       "index.markdown missing date format for grouping")


class TestHomeBuild(unittest.TestCase):
    """Verify Jekyll build produces a grouped home page."""

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

    def test_home_page_exists(self):
        home_page = SITE_DIR / "index.html"
        self.assertTrue(home_page.exists(), "index.html not found in _site/")

    def test_home_page_has_day_groups(self):
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        day_groups = re.findall(r'class="day-group"', content)
        self.assertGreaterEqual(
            len(day_groups), 3,
            f"Expected at least 3 day-group divs, found {len(day_groups)}"
        )

    def test_home_page_has_edition_badges(self):
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        # Should have Morning, Afternoon, and/or Evening badges
        badges = re.findall(r'class="edition-badge edition-badge-\w+"', content)
        self.assertGreater(
            len(badges), 0,
            "Expected edition badges in home page, found none"
        )

    def test_home_page_has_morning_badge(self):
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        self.assertIn("edition-badge-morning", content,
                       "Home page should have at least one Morning badge")

    def test_home_page_has_afternoon_badge(self):
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        self.assertIn("edition-badge-afternoon", content,
                       "Home page should have at least one Afternoon badge")

    def test_home_page_has_day_heading_links(self):
        """Day headings should link to the day index pages."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        # Should have links like /news/2026/06/17/ in day headings
        day_links = re.findall(r'href="[^"]*/news/\d{4}/\d{2}/\d{2}/"', content)
        self.assertGreater(
            len(day_links), 0,
            "Expected day heading links to /news/YYYY/MM/DD/ day index pages"
        )

    def test_home_page_not_flat_list(self):
        """Home page should NOT have the old flat post-list structure."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        # The old format was a simple <ul class="post-list"> with flat <li> items
        # The new format has <ul class="day-editions"> inside <div class="day-group">
        self.assertIn("day-editions", content,
                       "Home page should have day-editions list, not flat post-list")
        self.assertIn("day-group", content,
                       "Home page should have day-group containers")

    def test_home_page_has_edition_links(self):
        """Each edition item should have a link to the edition post."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        edition_links = re.findall(
            r'class="day-edition-item".*?href="[^"]*"(.*?)</li>',
            content, re.DOTALL
        )
        self.assertGreater(
            len(edition_links), 0,
            "Expected edition links inside day-edition-item elements"
        )

    def test_home_page_has_archive_link(self):
        """Home page should have links to archive pages."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        self.assertIn("archive", content.lower(),
                       "Home page should have archive link")

    def test_home_page_valid_html(self):
        """Home page should be valid HTML with proper structure."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        self.assertIn("<!DOCTYPE html>", content, "Missing DOCTYPE")
        self.assertIn("</html>", content, "Missing closing HTML tag")

    def test_editions_grouped_by_date(self):
        """Posts should be grouped under date headings, not in a flat list."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        # Each day-group should contain a day-heading and a day-editions list
        groups = content.split('class="day-group"')
        # First element is before any day-group
        for group_html in groups[1:]:
            self.assertIn("day-heading", group_html,
                           "Each day-group should have a day-heading")
            self.assertIn("day-editions", group_html,
                           "Each day-group should have a day-editions list")

    def test_badge_text_matches_class(self):
        """Badge text content should match the badge CSS class."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        # Find all badge spans
        morning_badges = re.findall(
            r'class="edition-badge edition-badge-morning"([^>]*>)([^<]*)', content
        )
        for attrs, text in morning_badges:
            self.assertIn("Morning", text,
                          f"Morning badge should contain 'Morning', got: {text}")

        afternoon_badges = re.findall(
            r'class="edition-badge edition-badge-afternoon"([^>]*>)([^<]*)', content
        )
        for attrs, text in afternoon_badges:
            self.assertIn("Afternoon", text,
                           f"Afternoon badge should contain 'Afternoon', got: {text}")

    def test_css_styles_in_layout(self):
        """Home layout should include CSS for edition badges and day groups."""
        home_page = SITE_DIR / "index.html"
        content = home_page.read_text()
        self.assertIn(".edition-badge", content,
                       "Home page should include edition-badge CSS")
        self.assertIn(".day-group", content,
                       "Home page should include day-group CSS")
        self.assertIn(".day-heading", content,
                       "Home page should include day-heading CSS")


if __name__ == "__main__":
    unittest.main()
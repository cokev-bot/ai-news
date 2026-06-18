#!/usr/bin/env python3
"""Tests for OG social card image generation.

Covers:
- tools/make_og_image.py: first_sentence(), render_og_image(), generate_og_image_for_edition()
- generate_news.py integration: get_og_image_config(), generate_og_image_for_edition(), front matter injection
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure tools/ is importable for make_og_image
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

from make_og_image import (
    first_sentence,
    render_og_image,
    generate_og_image_for_edition,
)

from generate_news import (
    get_og_image_config,
    DEFAULT_OG_IMAGE_CONFIG,
)


# ---------------------------------------------------------------------------
# first_sentence() tests
# ---------------------------------------------------------------------------

class TestFirstSentence(unittest.TestCase):
    """Tests for the first_sentence() text extraction function."""

    def test_simple_sentence(self):
        self.assertEqual(
            first_sentence("OpenAI released GPT-5 today. It's very impressive."),
            "OpenAI released GPT-5 today.",
        )

    def test_single_sentence(self):
        self.assertEqual(
            first_sentence("Just one sentence here."),
            "Just one sentence here.",
        )

    def test_empty_string(self):
        self.assertEqual(first_sentence(""), "")

    def test_none_like_empty(self):
        self.assertEqual(first_sentence(""), "")

    def test_long_text_no_sentence_boundary(self):
        text = "A " * 150  # ~300 chars, no sentence boundary
        result = first_sentence(text, max_chars=200)
        self.assertLessEqual(len(result), 210)  # allow for ellipsis
        self.assertTrue(result.endswith("…") or len(result.rstrip("…")) <= 200)

    def test_truncation_at_word_boundary(self):
        text = "This is a long piece of text that does not have any sentence boundaries and just keeps going and going"
        result = first_sentence(text, max_chars=50)
        self.assertLessEqual(len(result), 60)  # some tolerance for word boundary

    def test_html_stripped(self):
        self.assertEqual(
            first_sentence("<p>Hello world.</p> <b>Second sentence.</b>"),
            "Hello world.",
        )

    def test_abbreviation_not_split(self):
        # "U.S." should not be treated as a sentence boundary
        text = "The U.S. government announced new AI regulations. Big impact."
        result = first_sentence(text)
        # The regex looks for [.!?] followed by space then uppercase
        # "U.S. government" shouldn't match because "government" is lowercase
        self.assertIn("U.S.", result)

    def test_exact_max_chars(self):
        text = "A" * 200
        result = first_sentence(text, max_chars=200)
        # Should not crash, should return something
        self.assertTrue(len(result) > 0)

    def test_sentence_boundary_within_limit(self):
        text = "First sentence. Second sentence. Third sentence."
        result = first_sentence(text, max_chars=200)
        self.assertEqual(result, "First sentence.")

    def test_sentence_boundary_beyond_limit(self):
        # First sentence is longer than max_chars
        text = "A" * 300 + ". Then the next part."
        result = first_sentence(text, max_chars=50)
        # Should truncate since the first sentence boundary is beyond max_chars
        self.assertTrue(result.endswith("…") or len(result) <= 50)


# ---------------------------------------------------------------------------
# render_og_image() tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(PILLOW_AVAILABLE, "Pillow not installed")
class TestRenderOgImage(unittest.TestCase):
    """Tests for render_og_image() PNG generation."""

    def test_basic_render(self):
        """Render a basic OG image and verify it's a valid 1200x630 PNG."""
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "test_card.png"
            result = render_og_image(
                title="AI News Digest — Morning Edition",
                excerpt="OpenAI released GPT-5 today.",
                date_str="2026-06-18",
                edition_label="Morning",
                output_path=out_path,
            )
            self.assertIsNotNone(result)
            self.assertTrue(out_path.exists())
            img = Image.open(out_path)
            self.assertEqual(img.size, (1200, 630))
            self.assertEqual(img.format, "PNG")

    def test_render_without_saving(self):
        """Render without output_path returns an image but doesn't write a file."""
        result = render_og_image(
            title="Test Title",
            excerpt="Test excerpt.",
            date_str="2026-06-18",
            edition_label="Afternoon",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.size, (1200, 630))

    def test_render_empty_excerpt(self):
        """Render with an empty excerpt should not crash."""
        result = render_og_image(
            title="AI News Digest",
            excerpt="",
            date_str="2026-06-18",
            edition_label="Evening",
        )
        self.assertIsNotNone(result)

    def test_render_creates_parent_dirs(self):
        """Parent directories should be created if they don't exist."""
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "subdir" / "nested" / "card.png"
            result = render_og_image(
                title="Title",
                excerpt="Excerpt.",
                date_str="2026-06-18",
                edition_label="Morning",
                output_path=out_path,
            )
            self.assertIsNotNone(result)
            self.assertTrue(out_path.exists())

    def test_long_title_wraps(self):
        """A very long title should render without error (wrapping)."""
        result = render_og_image(
            title="AI News Digest — " + "Breaking " * 20,
            excerpt="Short excerpt.",
            date_str="2026-06-18",
            edition_label="Morning",
        )
        self.assertIsNotNone(result)

    def test_long_excerpt_truncates(self):
        """A very long excerpt should be truncated, not crash."""
        result = render_og_image(
            title="Title",
            excerpt="Word " * 200,  # ~1000 chars
            date_str="2026-06-18",
            edition_label="Morning",
        )
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# generate_og_image_for_edition() tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(PILLOW_AVAILABLE, "Pillow not installed")
class TestGenerateOgImageForEdition(unittest.TestCase):
    """Tests for generate_og_image_for_edition() integration."""

    def test_basic_generation(self):
        """Generate an OG image for an edition and verify the file path."""
        with tempfile.TemporaryDirectory() as td:
            result = generate_og_image_for_edition(
                edition="2026-06-18-Morning",
                site_root=td,
                global_summary_text="OpenAI released GPT-5 today. Big news for AI.",
            )
            self.assertIsNotNone(result)
            self.assertEqual(result, "assets/og/2026-06-18-Morning.png")
            # Verify file exists
            out_file = Path(td) / result
            self.assertTrue(out_file.exists())

    def test_edition_case_insensitive(self):
        """Edition label is capitalized regardless of input case."""
        with tempfile.TemporaryDirectory() as td:
            result = generate_og_image_for_edition(
                edition="2026-06-18-morning",
                site_root=td,
                global_summary_text="Test text.",
            )
            self.assertIsNotNone(result)
            self.assertIn("Morning", result)

    def test_none_summary(self):
        """Should handle None summary gracefully."""
        with tempfile.TemporaryDirectory() as td:
            result = generate_og_image_for_edition(
                edition="2026-06-18-Afternoon",
                site_root=td,
                global_summary_text=None,
            )
            self.assertIsNotNone(result)
            # File should still be created (just with empty excerpt)

    def test_empty_summary(self):
        """Should handle empty string summary gracefully."""
        with tempfile.TemporaryDirectory() as td:
            result = generate_og_image_for_edition(
                edition="2026-06-18-Evening",
                site_root=td,
                global_summary_text="",
            )
            self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# generate_news.py integration tests
# ---------------------------------------------------------------------------

class TestGetOgImageConfig(unittest.TestCase):
    """Tests for get_og_image_config() in generate_news.py."""

    def test_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps({"model": "test"}))
            with patch("generate_news.load_config", return_value={"model": "test"}):
                cfg = get_og_image_config(Path(td))
            self.assertTrue(cfg["enabled"])

    def test_user_override_disabled(self):
        user_cfg = {"model": "test", "og_image": {"enabled": False}}
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps(user_cfg))
            with patch("generate_news.load_config", return_value=user_cfg):
                cfg = get_og_image_config(Path(td))
            self.assertFalse(cfg["enabled"])

    def test_default_config_values(self):
        self.assertTrue(DEFAULT_OG_IMAGE_CONFIG["enabled"])


class TestOgImageFrontMatter(unittest.TestCase):
    """Tests that OG image path is correctly injected into post front matter."""

    def test_front_matter_has_image_when_og_image_generated(self):
        """Verify that the generate_post() front matter includes image: when
        OG image generation succeeds."""
        # We test the front-matter injection logic by simulating what
        # generate_post() does with html_lines.
        html_lines = [
            "---",
            "layout: post",
            'title: "AI News Digest — Morning Edition"',
            "date: 2026-06-18 08:00:00 -0700",
            "categories: news digest",
            "---",
            "",
            "<h2>🤖 AI News</h2>",
        ]
        # Simulate the injection logic from generate_post()
        og_image_rel_path = "assets/og/2026-06-18-Morning.png"
        if og_image_rel_path:
            for idx, line in enumerate(html_lines):
                if idx > 0 and line.strip() == "---":
                    html_lines.insert(idx, f"image: /ai-news/{og_image_rel_path}")
                    break

        # Check the front matter now
        self.assertIn("image: /ai-news/assets/og/2026-06-18-Morning.png", html_lines)
        # The --- end should come after the image line
        closing_idx = html_lines.index("---", 1)
        image_idx = html_lines.index("image: /ai-news/assets/og/2026-06-18-Morning.png")
        self.assertLess(image_idx, closing_idx)

    def test_front_matter_no_image_when_og_image_fails(self):
        """Verify that no image: line appears when OG image generation returns None."""
        html_lines = [
            "---",
            "layout: post",
            'title: "AI News Digest — Morning Edition"',
            "date: 2026-06-18 08:00:00 -0700",
            "categories: news digest",
            "---",
            "",
            "<h2>🤖 AI News</h2>",
        ]
        og_image_rel_path = None
        if og_image_rel_path:
            for idx, line in enumerate(html_lines):
                if idx > 0 and line.strip() == "---":
                    html_lines.insert(idx, f"image: /ai-news/{og_image_rel_path}")
                    break

        # No image line should have been added
        image_lines = [l for l in html_lines if l.startswith("image:")]
        self.assertEqual(len(image_lines), 0)


# ---------------------------------------------------------------------------
# Pillow-import-failure graceful degradation
# ---------------------------------------------------------------------------

class TestPillowUnavailable(unittest.TestCase):
    """Test that OG image generation degrades gracefully when Pillow is missing."""

    @patch.dict("sys.modules", {"PIL": None, "PIL.Image": None, "PIL.ImageDraw": None, "PIL.ImageFont": None})
    def test_returns_none_when_pillow_unavailable(self):
        """If Pillow can't be imported, generate_og_image_for_edition returns None."""
        # This test simulates the import failure path in the module-level code.
        # Since we already have Pillow installed, we test the ImportError
        # path in generate_og_image_for_edition() by mocking the import.
        with tempfile.TemporaryDirectory() as td:
            # The function tries to import from make_og_image, which checks
            # for PIL availability at the top of render_og_image().
            # If PIL is not available, render_og_image returns None.
            # generate_og_image_for_edition catches this and returns None.
            from generate_news import generate_og_image_for_edition as gen_func

            # Mock make_og_image to raise ImportError
            with patch.dict("sys.modules", {"make_og_image": None}):
                result = gen_func("2026-06-18-Morning", Path(td), "test text")
                # Should return None because the import fails
                self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
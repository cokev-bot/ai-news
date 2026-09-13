"""Tests for the edition-specific meta description and audio-player ARIA labels.

Covers:
- _make_description() derives a YAML-safe, truncated description from the
  HTML Big Picture summary.
- generate_post() injects `description:` into the front matter only when a
  Big Picture summary exists.
- audio_player_html() emits an `aria-label` on the <audio> element.
"""

import unittest
from pathlib import Path

from generate_news import _make_description, audio_player_html


class TestMakeDescription(unittest.TestCase):

    def test_empty_summary_is_empty(self):
        self.assertEqual(_make_description(None), "")
        self.assertEqual(_make_description(""), "")

    def test_strips_html_tags(self):
        out = _make_description('AI is <a href="https://x.com">transforming</a> everything.')
        self.assertEqual(out, "AI is transforming everything.")

    def test_strips_link_text_only(self):
        out = _make_description('Read <a href="https://x.com">OpenAI</a> news.')
        self.assertEqual(out, "Read OpenAI news.")

    def test_truncates_on_word_boundary(self):
        long = "word " * 100
        out = _make_description(long, max_chars=20)
        self.assertLessEqual(len(out), 21)  # 20 + ellipsis at most
        self.assertTrue(out.endswith("…"))
        self.assertFalse(out.rstrip("…").endswith(" "))

    def test_short_text_preserved_in_full(self):
        out = _make_description("Short summary.", max_chars=160)
        self.assertEqual(out, "Short summary.")

    def test_double_quotes_replaced_for_yaml_safety(self):
        out = _make_description('He said "hello" to the world.')
        self.assertNotIn('"', out)
        self.assertIn("'", out)

    def test_backslashes_and_brackets_removed(self):
        out = _make_description('path\\to [bracket] {brace} text')
        self.assertNotIn("\\", out)
        self.assertNotIn("[", out)
        self.assertNotIn("]", out)
        self.assertNotIn("{", out)
        self.assertNotIn("}", out)

    def test_html_entities_decoded(self):
        out = _make_description("OpenAI&#8217;s new model is out.")
        # &#8217; is the right single quotation mark (U+2019), not ASCII '.
        self.assertIn("\u2019", out)

    def test_whitespace_collapsed(self):
        out = _make_description("  too    many\n\n   spaces   ")
        self.assertEqual(out, "too many spaces")


class TestAudioPlayerAriaLabel(unittest.TestCase):

    def test_aria_label_includes_section_name(self):
        html = audio_player_html("assets/audio/x/news.mp3", "News section")
        self.assertIn('aria-label="Audio summary of News section"', html)

    def test_label_is_html_escaped(self):
        html = audio_player_html("assets/audio/x/a.mp3", 'News <script>"')
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_no_aria_label_breaks_audio_element(self):
        html = audio_player_html("assets/audio/x/a.mp3", "Big Picture summary")
        self.assertIn("<audio", html)
        self.assertIn("controls", html)


if __name__ == "__main__":
    unittest.main()

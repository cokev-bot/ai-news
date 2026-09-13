"""Tests for the per-edition reading-time estimate and per-section count badges.

Covers word counting (HTML-aware), the minute estimate (rounding, empty
input, custom words-per-minute), the ``~N min read`` formatting, and the
``<h2>Section <span class="section-count">(N)</span></h2>`` heading.
"""

import unittest

from generate_news import (
    READING_WORDS_PER_MINUTE,
    compute_reading_time_minutes,
    count_words,
    format_reading_time,
    format_section_heading,
)


class TestCountWords(unittest.TestCase):
    """Unit tests for count_words."""

    def test_plain_text(self):
        self.assertEqual(count_words("one two three"), 3)

    def test_empty_and_none(self):
        self.assertEqual(count_words(""), 0)
        self.assertEqual(count_words(None), 0)

    def test_whitespace_only(self):
        self.assertEqual(count_words("   \n\t  "), 0)

    def test_html_tags_are_not_counted(self):
        """Tag names and attribute values must not inflate the word count."""
        self.assertEqual(
            count_words('<p class="summary">hello world</p>'), 2
        )

    def test_anchor_text_counts_as_one_word(self):
        html = '<a href="https://example.com/a-b-c">OpenAI</a>'
        self.assertEqual(count_words(html), 1)

    def test_citation_links_in_summary(self):
        html = 'A story broke <a href="https://x.com/foo/status/1">Reuters</a> today.'
        # A story broke Reuters today. -> 5 words
        self.assertEqual(count_words(html), 5)

    def test_hyphenated_url_words_do_not_count(self):
        """Hyphens inside URLs are not word separators for our purposes."""
        html = '<a href="https://example.com/the-quick-brown-fox">Source</a>'
        self.assertEqual(count_words(html), 1)

    def test_punctuation_only(self):
        self.assertEqual(count_words("... --- ???"), 0)

    def test_numbers_and_apostrophes_count(self):
        # "OpenAI's" is one word; "2026" is one word.
        self.assertEqual(count_words("OpenAI's 2026 roadmap"), 3)

    def test_entities_are_stripped_with_tags(self):
        """HTML entities decode away instead of counting as words."""
        html = "<p>GPT&amp;Gemini</p>"
        self.assertEqual(count_words(html), 2)

    def test_numeric_entities_do_not_count(self):
        self.assertEqual(count_words("it&#8217;s here"), 2)


class TestFormatReadingTime(unittest.TestCase):
    """Unit tests for format_reading_time."""

    def test_typical(self):
        self.assertEqual(format_reading_time(4), "~4 min read")

    def test_zero(self):
        self.assertEqual(format_reading_time(0), "~0 min read")

    def test_single_minute(self):
        self.assertEqual(format_reading_time(1), "~1 min read")

    def test_float_is_truncated_to_int(self):
        self.assertEqual(format_reading_time(4.9), "~4 min read")


class TestComputeReadingTimeMinutes(unittest.TestCase):
    """Unit tests for compute_reading_time_minutes."""

    def _make_item(self, title="", description="", source="TestFeed"):
        return {"title": title, "description": description, "source": source}

    def test_empty_inputs_return_zero(self):
        """No content at all → 0, so the caller omits the fragment."""
        self.assertEqual(compute_reading_time_minutes(), 0)
        self.assertEqual(
            compute_reading_time_minutes(section_summaries={},
                                         global_summary_text="",
                                         subsection_articles={}),
            0,
        )

    def test_empty_article_list_return_zero(self):
        self.assertEqual(
            compute_reading_time_minutes(subsection_articles={"News": []}),
            0,
        )

    def test_rounds_up_partial_minutes(self):
        """231 words at the default 230 wpm rounds up to 2 minutes."""
        text = " ".join(["word"] * 231)
        self.assertEqual(
            compute_reading_time_minutes(global_summary_text=text), 2
        )

    def test_exact_minute_boundary(self):
        """Exactly 230 words is 1 minute, not 2."""
        text = " ".join(["word"] * 230)
        self.assertEqual(
            compute_reading_time_minutes(global_summary_text=text), 1
        )

    def test_minimum_of_one_minute(self):
        """A single word still reports 1 minute rather than 0."""
        self.assertEqual(
            compute_reading_time_minutes(global_summary_text="hello"), 1
        )

    def test_global_summary_is_counted(self):
        text = " ".join(["word"] * 100)
        self.assertEqual(
            compute_reading_time_minutes(global_summary_text=text), 1
        )

    def test_section_summaries_are_summed(self):
        summaries = {"News": " ".join(["w"] * 100), "AI Labs": " ".join(["w"] * 200)}
        # 300 words / 230 wpm -> 2 minutes
        self.assertEqual(
            compute_reading_time_minutes(section_summaries=summaries), 2
        )

    def test_item_titles_and_descriptions_are_counted(self):
        articles = {
            "News": [
                self._make_item(title="Breaking AI news", description=" ".join(["w"] * 50)),
                self._make_item(title="Another story", description=" ".join(["w"] * 50)),
            ]
        }
        # 3 + 50 + 2 + 50 = 105 words -> 1 minute
        self.assertEqual(
            compute_reading_time_minutes(subsection_articles=articles), 1
        )

    def test_items_without_description_are_tolerated(self):
        articles = {"News": [{"title": "Only a title", "source": "X"}]}
        self.assertEqual(
            compute_reading_time_minutes(subsection_articles=articles), 1
        )

    def test_all_sources_combined(self):
        """Big Picture + sections + items all contribute to one total."""
        articles = {"News": [self._make_item(title=" ".join(["w"] * 230))]}
        summaries = {"News": " ".join(["w"] * 230)}
        total = compute_reading_time_minutes(
            section_summaries=summaries,
            global_summary_text=" ".join(["w"] * 230),
            subsection_articles=articles,
        )
        # 690 words / 230 wpm = 3 minutes exactly
        self.assertEqual(total, 3)

    def test_custom_words_per_minute(self):
        text = " ".join(["w"] * 100)
        # Slower reader (100 wpm) → 1 minute for 100 words
        self.assertEqual(
            compute_reading_time_minutes(
                global_summary_text=text, words_per_minute=100
            ),
            1,
        )
        # At 50 wpm the same 100 words take 2 minutes
        self.assertEqual(
            compute_reading_time_minutes(
                global_summary_text=text, words_per_minute=50
            ),
            2,
        )

    def test_invalid_words_per_minute_falls_back_to_default(self):
        text = " ".join(["w"] * 230)
        for bad in (0, -5, None):
            self.assertEqual(
                compute_reading_time_minutes(
                    global_summary_text=text, words_per_minute=bad
                ),
                1,
            )

    def test_reading_minutes_is_an_int(self):
        result = compute_reading_time_minutes(global_summary_text="hello world")
        self.assertIsInstance(result, int)

    def test_html_in_summaries_is_not_double_counted(self):
        html_summary = '<a href="https://example.com">Reuters</a> reports news'
        self.assertEqual(
            compute_reading_time_minutes(section_summaries={"News": html_summary}),
            1,
        )

    def test_default_wpm_constant(self):
        self.assertEqual(READING_WORDS_PER_MINUTE, 230)


class TestFormatSectionHeading(unittest.TestCase):
    """Unit tests for format_section_heading.

    Headings carry a slug ``id`` (``<h2 id="news">``) so section anchors exist
    for in-page navigation and for the static JSON API's per-section ``url``.
    """

    def test_typical(self):
        self.assertEqual(
            format_section_heading("News", 3),
            '<h2 id="news">News <span class="section-count">(3)</span></h2>',
        )

    def test_zero_count(self):
        self.assertEqual(
            format_section_heading("Benchmarks", 0),
            '<h2 id="benchmarks">Benchmarks <span class="section-count">(0)</span></h2>',
        )

    def test_multiword_section_title(self):
        self.assertEqual(
            format_section_heading("AI Labs", 38),
            '<h2 id="ai-labs">AI Labs <span class="section-count">(38)</span></h2>',
        )

    def test_float_count_is_truncated(self):
        self.assertEqual(
            format_section_heading("News", 3.7),
            '<h2 id="news">News <span class="section-count">(3)</span></h2>',
        )

    def test_heading_still_contains_plain_title(self):
        """Existing tooling that greps for the title still finds it."""
        heading = format_section_heading("Developers", 7)
        self.assertIn("Developers", heading)
        self.assertTrue(heading.startswith("<h2"))
        self.assertTrue(heading.endswith("</h2>"))


class TestHeaderLineIntegration(unittest.TestCase):
    """Integration-level checks on the header line's shape."""

    def _build_header(self, total_feeds, num_sources, num_items, tally, minutes):
        fragments = [
            "Scanning {} feeds".format(total_feeds),
            "{} accounts posted".format(num_sources),
            "{} items".format(num_items),
            tally,
        ]
        if minutes > 0:
            fragments.append(format_reading_time(minutes))
        return "<p>{}</p>".format(" · ".join(fragments))

    def test_header_includes_reading_time(self):
        header = self._build_header(
            31, 5, 6, "3 fresh, 1 stale, 2 from yesterday", 4
        )
        self.assertIn("Scanning 31 feeds", header)
        self.assertIn("5 accounts posted", header)
        self.assertIn("6 items", header)
        self.assertIn("3 fresh, 1 stale, 2 from yesterday", header)
        self.assertIn("~4 min read", header)

    def test_reading_time_is_last_fragment(self):
        header = self._build_header(31, 5, 6, "1 fresh, 0 stale, 0 from yesterday", 4)
        self.assertTrue(header.endswith("~4 min read</p>"))

    def test_reading_time_fragment_omitted_when_zero(self):
        header = self._build_header(31, 0, 0, "0 fresh, 0 stale, 0 from yesterday", 0)
        self.assertNotIn("min read", header)
        self.assertIn("Scanning 31 feeds", header)

    def test_reading_time_matches_estimate(self):
        """The fragment the header renders agrees with the estimator."""
        words = " ".join(["w"] * 920)
        minutes = compute_reading_time_minutes(global_summary_text=words)
        self.assertEqual(minutes, 4)
        header = self._build_header(31, 5, 6, "6 fresh, 0 stale, 0 from yesterday", minutes)
        self.assertIn(format_reading_time(4), header)

    def test_reading_time_rounds_up_not_down(self):
        """700 words at 230 wpm is 3.04 min → reported as ~4, never ~3."""
        words = " ".join(["w"] * 700)
        minutes = compute_reading_time_minutes(global_summary_text=words)
        self.assertEqual(minutes, 4)


if __name__ == "__main__":
    unittest.main()

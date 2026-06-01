r"""Edge-case tests for linkify_urls() in generate_news.py.

The URL regex used by linkify_urls is:
    r'https?://[^\s<>"\')\]]+'

It stops at: whitespace, <, >, ", ', ), ]. It does NOT stop at: , ; } ! ?
This is intentional in some cases (parentheses are valid in URLs) but
leaky in others (a trailing comma in "see https://x.com/foo, thanks"
captures the comma into the URL).

These tests pin current behavior so future changes are intentional.
"""
import unittest
from generate_news import linkify_urls
import unittest
from generate_news import linkify_urls


class TestLinkifyUrlsEdges(unittest.TestCase):

    def test_simple_url(self):
        out = linkify_urls("Visit https://example.com today")
        self.assertIn('<a href="https://example.com">https://example.com</a>', out)

    def test_url_in_parentheses_not_double_linked(self):
        """A URL wrapped in () is captured up to (not including) )."""
        out = linkify_urls("(see https://example.com)")
        # The ( is preserved outside the anchor
        self.assertIn('(see', out)
        self.assertIn('<a href="https://example.com">https://example.com</a>', out)
        # The closing ) is preserved
        self.assertIn(')', out)

    def test_nitter_url_rewritten_to_x(self):
        out = linkify_urls("see https://nitter.net/foo/status/123")
        # nitter.net URLs in the visible link text are rewritten to x.com
        self.assertIn('https://x.com/foo/status/123', out)
        # but nitter is replaced inside the visible text
        self.assertNotIn('nitter.net/foo', out.replace('nitter.net"', ''))

    def test_existing_anchor_not_double_linkified(self):
        text = '<a href="https://google.com">Google</a>'
        self.assertEqual(linkify_urls(text), text)

    def test_url_followed_by_period(self):
        """Period at end of sentence is captured. KNOWN BUG: should be
        documented. Current regex does not exclude '.', so 'https://x.com.'
        becomes the URL."""
        out = linkify_urls("see https://x.com.")
        # This documents the current behavior — period is included
        self.assertIn('<a href="https://x.com.">https://x.com.</a>', out)
        # If you want to fix this, update the regex and this test in one PR.

    def test_url_followed_by_comma(self):
        """Comma after a URL is captured. KNOWN BUG/EDGE CASE."""
        out = linkify_urls("see https://x.com/foo, and https://y.com")
        # First URL has comma captured (current behavior)
        self.assertIn('<a href="https://x.com/foo,">https://x.com/foo,</a>', out)
        # Second URL is clean
        self.assertIn('<a href="https://y.com">https://y.com</a>', out)

    def test_url_with_query_and_fragment(self):
        out = linkify_urls("https://example.com/foo?a=1&b=2#section")
        # Note: linkify_urls does NOT HTML-escape ampersands. This is a
        # pre-existing behavior; documenting here so any change to it is
        # caught and made intentional.
        self.assertIn(
            '<a href="https://example.com/foo?a=1&b=2#section">'
            'https://example.com/foo?a=1&b=2#section</a>',
            out,
        )

    def test_empty_string(self):
        self.assertEqual(linkify_urls(""), "")

    def test_no_url(self):
        self.assertEqual(linkify_urls("just plain text"), "just plain text")


if __name__ == "__main__":
    unittest.main()

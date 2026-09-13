import unittest
from generate_news import text_similarity, linkify_urls, nitter_to_x, is_nitter_link


class TestAiNewsLogic(unittest.TestCase):

    def test_text_similarity(self):
        # Identical strings
        self.assertAlmostEqual(text_similarity("Hello world", "Hello world"), 1.0)
        # Totally different strings
        self.assertEqual(text_similarity("Apple", "Banana"), 0.0)
        # Partial overlap
        self.assertTrue(0.0 < text_similarity("The quick brown fox", "The slow brown dog") < 1.0)
        # Case insensitive
        self.assertAlmostEqual(text_similarity("HELLO WORLD", "hello world"), 1.0)

    def test_nitter_conversion(self):
        self.assertTrue(is_nitter_link("https://nitter.net/user/status/123"))
        self.assertFalse(is_nitter_link("https://x.com/user/status/123"))
        self.assertEqual(nitter_to_x("https://nitter.net/user/status/123"), "https://x.com/user/status/123")

    def test_linkify_urls(self):
        text = "Check this out: https://example.com and https://nitter.net/test"
        result = linkify_urls(text)
        self.assertIn('<a href="https://example.com">https://example.com</a>', result)
        self.assertIn('<a href="https://x.com/test">https://x.com/test</a>', result)

        # Test that existing anchors are not double-linkified
        already_linked = '<a href="https://google.com">Google</a>'
        self.assertEqual(linkify_urls(already_linked), already_linked)


if __name__ == "__main__":
    unittest.main()

"""Tests for parallel RSS feed fetching in generate_news.py."""
import unittest
from unittest.mock import patch, MagicMock
from generate_news import fetch_all_feeds, MAX_FEED_WORKERS


class TestFetchAllFeeds(unittest.TestCase):
    """Tests for the fetch_all_feeds helper."""

    def _make_sections(self, n_subsections=2, feeds_per_sub=2):
        """Build a minimal sections list for testing.

        Each subsection gets N feeds named feed_<sub>_0, feed_<sub>_1, etc.
        """
        sections = []
        for si in range(1):  # single top-level section
            subs = []
            for sub_i in range(n_subsections):
                feeds = {}
                feeds_alts = {}
                for fi in range(feeds_per_sub):
                    fname = f"feed_{sub_i}_{fi}"
                    feeds[fname] = f"https://example.com/{fname}"
                    # Give odd-indexed feeds one fallback each
                    if fi % 2 == 1:
                        feeds_alts[fname] = [f"https://fallback.example.com/{fname}"]
                subs.append({
                    "title": f"Subsection_{sub_i}",
                    "feeds": feeds,
                    "feeds_alts": feeds_alts,
                })
            sections.append({"subsections": subs})
        return sections

    @patch("generate_news.fetch_feed")
    def test_returns_correct_structure(self, mock_fetch):
        """fetch_all_feeds returns subsection_title → [(feed_name, articles)] in order."""
        mock_fetch.return_value = [{"title": "Article", "link": "https://a.com", "source": "test"}]

        sections = self._make_sections(n_subsections=2, feeds_per_sub=2)
        result = fetch_all_feeds(sections)

        # Should have 2 subsection keys
        self.assertEqual(set(result.keys()), {"Subsection_0", "Subsection_1"})

        # Each subsection should have 2 (feed_name, articles) tuples
        for sub_key in result:
            self.assertEqual(len(result[sub_key]), 2)
            for feed_name, articles in result[sub_key]:
                self.assertIsInstance(feed_name, str)
                self.assertIsInstance(articles, list)
                self.assertEqual(len(articles), 1)
                self.assertEqual(articles[0]["title"], "Article")

    @patch("generate_news.fetch_feed")
    def test_empty_feeds(self, mock_fetch):
        """fetch_all_feeds works when all feeds return empty lists."""
        mock_fetch.return_value = []

        sections = self._make_sections(n_subsections=3, feeds_per_sub=1)
        result = fetch_all_feeds(sections)

        self.assertEqual(set(result.keys()), {"Subsection_0", "Subsection_1", "Subsection_2"})
        for sub_key in result:
            self.assertEqual(len(result[sub_key]), 1)
            _feed_name, articles = result[sub_key][0]
            self.assertEqual(articles, [])

    @patch("generate_news.fetch_feed")
    def test_handles_exceptions_gracefully(self, mock_fetch):
        """If fetch_feed raises (it shouldn't, but we guard), no abort."""
        call_count = 0

        def side_effect(name, url, fallbacks=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise RuntimeError("network failure")
            return [{"title": f"OK-{name}", "link": f"https://{name}.com", "source": name}]

        mock_fetch.side_effect = side_effect

        sections = self._make_sections(n_subsections=2, feeds_per_sub=2)
        result = fetch_all_feeds(sections)

        # Should have all 4 feed entries, 2 succeeded, 2 caught-as-empty
        total_feeds = sum(len(v) for v in result.values())
        self.assertEqual(total_feeds, 4)

        # Successful feeds have 1 article each
        total_articles = sum(len(arts) for _, arts in result["Subsection_0"] + result["Subsection_1"])
        # Only 2 of 4 feeds succeeded (odd call_count)
        self.assertEqual(total_articles, 2)

    @patch("generate_news.fetch_feed")
    def test_workers_capped(self, mock_fetch):
        """MAX_FEED_WORKERS caps the pool size."""
        mock_fetch.return_value = []

        many_sections = self._make_sections(n_subsections=10, feeds_per_sub=5)
        # 50 feeds total, should cap at MAX_FEED_WORKERS
        with patch("generate_news.ThreadPoolExecutor") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)

            # Make submit return futures that resolve immediately
            from concurrent.futures import Future
            def make_future(*args, **kwargs):
                f = Future()
                f.set_result([])
                return f
            mock_pool.submit.side_effect = make_future
            mock_pool_cls.return_value = mock_pool

            fetch_all_feeds(many_sections)

            # Check ThreadPoolExecutor was called with max_workers <= MAX_FEED_WORKERS
            call_kwargs = mock_pool_cls.call_args
            workers = call_kwargs.kwargs.get("max_workers") or call_kwargs[1].get("max_workers")
            self.assertLessEqual(workers, MAX_FEED_WORKERS)

    @patch("generate_news.fetch_feed")
    def test_canonical_order_preserved(self, mock_fetch):
        """Results are returned in section/subsection/feed order from sections.json."""
        # Each feed returns a unique article so we can check ordering
        def side_effect(name, url, fallbacks=None, **kwargs):
            return [{"title": f"Article-from-{name}", "link": f"https://{name}", "source": name}]

        mock_fetch.side_effect = side_effect

        sections = self._make_sections(n_subsections=2, feeds_per_sub=3)
        result = fetch_all_feeds(sections)

        # Check feed names appear in the order they were defined
        sub0_names = [fn for fn, _ in result["Subsection_0"]]
        self.assertEqual(sub0_names, ["feed_0_0", "feed_0_1", "feed_0_2"])

        sub1_names = [fn for fn, _ in result["Subsection_1"]]
        self.assertEqual(sub1_names, ["feed_1_0", "feed_1_1", "feed_1_2"])


if __name__ == "__main__":
    unittest.main()
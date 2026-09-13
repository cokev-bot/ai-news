"""Tests for the "no new articles" exit contract in generate_post().

Background (2026-09-13): with only 8 of 34 feeds live, the 00:00 UTC Evening
edition found 66 articles, kept 0 (all already published by the Morning run
inside the 24h cross-edition window) and exited 1. run_edition.sh runs under
`set -e`, so it aborted BEFORE the Jekyll build and commit: no post, no
commit, and the site silently skipped a slot. The failure surfaced in cron as
a bare exit code, with the real cause (window exhausted) invisible.

The fix distinguishes the two zero-item cases, because conflating them is
dangerous in both directions:

  * a genuinely quiet edition must exit 0, so a routine all-deduplicated slot
    is not reported as a failure and does not lose its post;
  * a degraded run (sources unreachable) must still exit non-zero, because
    that is the "26 feeds dead but everything looks healthy" bug class this
    whole line of work exists to eliminate.

The discriminator is fetch health, not item count. These tests pin both sides
plus the deliberate bias: when we cannot prove ANY feed is healthy, we fail.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import generate_news  # noqa: E402
from generate_news import DEFAULT_CONFIG, generate_post  # noqa: E402


def _make_site_root(tmp_path: Path) -> Path:
    site_root = tmp_path / "site"
    site_root.mkdir()
    (site_root / "config.json").write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
    (site_root / "sections.json").write_text(json.dumps({
        "source_urls": {"FeedA": "https://a"},
        "sections": [
            {"title": "News", "subsections": [
                {"title": "SubA", "feeds": {"FeedA": "https://a/rss"}},
            ]},
        ],
    }), encoding="utf-8")
    (site_root / "summary_prompt.txt").write_text("Summarize: ", encoding="utf-8")
    (site_root / "_posts").mkdir()
    return site_root


def _article(title="A story", link="https://a/story"):
    return {
        "title": title, "link": link, "source": "FeedA",
        "description": "d", "pub": "", "pub_dt": None,
    }


class _Base(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="noarticles-")
        self.site_root = _make_site_root(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _run(self, *, fecundity_ok, articles, dedup_everything):
        """Invoke generate_post with controlled fetch health and dedup outcome."""
        def fake_fetch_all_feeds(sections, **kwargs):
            sink = kwargs.get("health_sink")
            if sink is not None:
                sink.append({"name": "FeedA", "url": "https://a/rss",
                             "ok": fecundity_ok,
                             "error": None if fecundity_ok else "all 1 URL(s) failed"})
            return {"SubA": [("FeedA", articles)]}

        with patch("generate_news.fetch_all_feeds", side_effect=fake_fetch_all_feeds), \
             patch("generate_news.is_duplicate", return_value=dedup_everything), \
             patch("generate_news._query_ollama", return_value="Summary."), \
             patch("generate_news.generate_edition_audio", return_value={}), \
             patch("generate_news.generate_og_image_for_edition", return_value=None):
            return generate_post("2026-09-13-evening", self.site_root)


class TestQuietEditionIsSuccess(_Base):
    """Sources reachable, nothing new → exit 0, no post, no error."""

    def test_all_deduplicated_with_healthy_feeds_returns_true(self):
        result = self._run(fecundity_ok=True,
                           articles=[_article()],
                           dedup_everything=True)
        self.assertTrue(result, "a quiet edition must not be reported as a failure")

    def test_quiet_edition_writes_no_post(self):
        self._run(fecundity_ok=True, articles=[_article()], dedup_everything=True)
        self.assertEqual(list((self.site_root / "_posts").glob("*.html")), [],
                         "a quiet edition must not emit an empty post")

    def test_quiet_edition_with_no_articles_at_all_is_success(self):
        """Feeds fetched OK but returned nothing in the window."""
        result = self._run(fecundity_ok=True, articles=[], dedup_everything=True)
        self.assertTrue(result)

    def test_quiet_edition_does_not_pollute_dedup_state(self):
        self._run(fecundity_ok=True, articles=[_article()], dedup_everything=True)
        state_path = self.site_root / ".news_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state.get("seen_links", {}), {})


class TestDegradedEditionStillFails(_Base):
    """Every source unreachable → must still exit non-zero."""

    def test_all_feeds_failed_returns_false(self):
        result = self._run(fecundity_ok=False,
                           articles=[_article()],
                           dedup_everything=True)
        self.assertFalse(result,
                         "an outage must never be reported as a quiet day")

    def test_all_feeds_failed_with_no_articles_returns_false(self):
        result = self._run(fecundity_ok=False, articles=[], dedup_everything=True)
        self.assertFalse(result)

    def test_degraded_run_writes_no_post(self):
        self._run(fecundity_ok=False, articles=[_article()], dedup_everything=True)
        self.assertEqual(list((self.site_root / "_posts").glob("*.html")), [])


class TestBiasTowardReportingOutages(_Base):
    """When health cannot be established, prefer failing over hiding."""

    def test_empty_health_sink_is_treated_as_failure(self):
        """No health data means we cannot prove any feed is healthy."""
        def fake_fetch_all_feeds(sections, **kwargs):
            return {"SubA": [("FeedA", [_article()])]}

        with patch("generate_news.fetch_all_feeds", side_effect=fake_fetch_all_feeds), \
             patch("generate_news.is_duplicate", return_value=True), \
             patch("generate_news._query_ollama", return_value="Summary."), \
             patch("generate_news.generate_edition_audio", return_value={}), \
             patch("generate_news.generate_og_image_for_edition", return_value=None):
            result = generate_post("2026-09-13-evening", self.site_root)
        self.assertFalse(result, "absent health data must not read as healthy")


class TestHealthyEditionStillPublishes(_Base):
    """The ordinary path is untouched: new items publish and return True."""

    def test_new_articles_publish_and_return_true(self):
        result = self._run(fecundity_ok=True,
                           articles=[_article()],
                           dedup_everything=False)
        self.assertTrue(result)
        posts = list((self.site_root / "_posts").glob("*.html"))
        self.assertEqual(len(posts), 1)
        self.assertIn("A story", posts[0].read_text(encoding="utf-8"))


class TestCliExitCodeMapping(unittest.TestCase):
    """The regression that started this: the process exit code must follow
    generate_post()'s contract, because run_edition.sh runs under `set -e`."""

    def _run_cli(self, generate_post_returns: bool) -> int:
        """Execute the real __main__ block from generate_news.py in a subprocess.

        Rather than re-implementing the CLI here (which would let the test and
        the script drift apart), this compiles and execs the actual
        `if __name__ == "__main__":` block from the source file, with
        generate_post stubbed to the return value under test.
        """
        import subprocess
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
            "import generate_news as g\n"
            f"g.generate_post = lambda *a, **k: {generate_post_returns!r}\n"
            "sys.argv = ['generate_news.py', '2026-09-13-evening', '/tmp']\n"
            f"src = open({str(PROJECT_ROOT / 'generate_news.py')!r}).read()\n"
            "marker = 'if __name__ == \"__main__\":'\n"
            "main_src = marker + src.split(marker, 1)[1]\n"
            "exec(compile(main_src, 'generate_news.py', 'exec'),\n"
            "     {**g.__dict__, '__name__': '__main__'})\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
        return proc.returncode

    def test_cli_exits_zero_when_generate_post_succeeds(self):
        self.assertEqual(self._run_cli(True), 0)

    def test_cli_exits_one_when_generate_post_fails(self):
        self.assertEqual(self._run_cli(False), 1)

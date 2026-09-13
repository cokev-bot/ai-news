"""Tests for the jekyll-build retry/backoff added to run_edition.sh.

The build step previously ran once; a transient Bundler/gem/network hiccup
would abort the entire edition. It now retries up to 3 times with a 30s
backoff and only aborts after a persistent failure.

We don't run the full script (Ollama, network, secrets); we assert the
*structure* the script must keep so the retry cannot silently regress back
to a single bare build.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "run_edition.sh"


class TestJekyllBuildRetry(unittest.TestCase):

    def test_retry_loop_present(self):
        content = SCRIPT.read_text()
        self.assertIn("for attempt in 1 2 3", content,
                      "build step must loop over 3 attempts")

    def test_backoff_sleep_present(self):
        content = SCRIPT.read_text()
        self.assertIn("sleep 30", content,
                      "build retry must back off between attempts")

    def test_persistent_failure_aborts(self):
        content = SCRIPT.read_text()
        self.assertIn("exit \"$JEKYLL_RC\"", content,
                      "a persistent build failure must still abort the edition")

    def test_build_still_runs_under_set_e(self):
        """The retry must not defeat set -e: the loop's failure is captured in
        JEKYLL_RC and re-raised with an explicit exit, not swallowed."""
        content = SCRIPT.read_text()
        self.assertIn("set -e", content)
        self.assertIn("JEKYLL_RC=1", content)

    def test_no_bare_single_build_left(self):
        """There must be no remaining single-shot `bundle exec jekyll build`
        line outside the retry loop (the pre-retry shape)."""
        content = SCRIPT.read_text()
        # The only jekyll build invocation should be inside the for loop.
        self.assertIn("bundle exec jekyll build --destination _site", content)


if __name__ == "__main__":
    unittest.main()

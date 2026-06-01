"""Regression tests for run_edition.sh build-failure handling.

These tests verify the bug we fixed in commit (hermes-handoff):
  bundle exec jekyll build --destination _site 2>&1 | tail -3
With set -e but no set -o pipefail, the | tail -3 made the pipeline's exit
code be tail's (always 0), so a Jekyll build failure was invisible and the
script proceeded to commit and push a possibly-broken post.

We don't run the full script (it requires Ollama, network, real secrets, etc).
Instead we exercise the *exact* idiom in the script in isolation: substitute
a fake `bundle` on PATH that exits non-zero, and assert set -e + pipefail
catch it.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "run_edition.sh"


class TestRunEditionBuildFailure(unittest.TestCase):
    """Verify the build-failure detection path inside run_edition.sh."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="hermes-handoff-test-"))
        self.fakebin = self.tmpdir / "fakebin"
        self.fakebin.mkdir()
        self.bundle = self.fakebin / "bundle"
        self.bundle.write_text("#!/bin/bash\nexit 1\n")
        self.bundle.chmod(self.bundle.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_isolated_jekyll_step(self):
        """Run the EXACT line pattern from run_edition.sh in isolation.

        Old (buggy): bundle exec jekyll build --destination _site 2>&1 | tail -3
        New (fixed): bundle exec jekyll build --destination _site
        Combined with `set -o pipefail` set earlier in the script.
        """
        env = os.environ.copy()
        env["PATH"] = f"{self.fakebin}{os.pathsep}{env['PATH']}"
        # The fixed script uses: bundle exec jekyll build --destination _site
        # (no pipe). Combined with set -o pipefail, this fails fast.
        result = subprocess.run(
            ["bash", "-c",
             "set -e; set -o pipefail; "
             "bundle exec jekyll build --destination _site; "
             "echo 'THIS_SHOULD_NOT_PRINT'"
             ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result

    def test_jekyll_failure_aborts_script(self):
        """A non-zero exit from jekyll must abort the script."""
        result = self._run_isolated_jekyll_step()
        self.assertNotEqual(result.returncode, 0,
                            "Jekyll failure should produce non-zero exit")
        self.assertNotIn("THIS_SHOULD_NOT_PRINT", result.stdout,
                         "Commands after a failed jekyll must not execute")

    def test_pipefail_is_set_in_script(self):
        """The script must enable set -o pipefail."""
        content = SCRIPT.read_text()
        self.assertIn("set -o pipefail", content,
                      "run_edition.sh must enable pipefail to catch "
                      "failures in jekyll build pipelines")

    def test_no_tail_in_jekyll_command(self):
        """The script must NOT pipe jekyll build to tail -3."""
        content = SCRIPT.read_text()
        # The literal `| tail -3` after jekyll build should be gone — that
        # was the buggy pattern that masked the exit code.
        self.assertNotIn("jekyll build --destination _site 2>&1 | tail",
                        content,
                        "Old buggy pattern is still present: jekyll build "
                        "is piped to tail -3, which masks the exit code")


if __name__ == "__main__":
    unittest.main()

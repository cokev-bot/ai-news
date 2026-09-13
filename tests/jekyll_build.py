"""Shared Jekyll build helper for the test suite.

Several test modules (test_feed, test_archive, test_day_index,
test_home_page) each rebuild the site to ``_site/`` in their ``setUpClass``
and assert on different artifacts of the *same* build. Each build costs ~4s,
so running them independently turns one build into four or five.

This module runs ``bundle exec jekyll build`` exactly once per test process
and caches the ``CompletedProcess`` result, so every module that needs a
fresh ``_site/`` shares a single build. The build is deterministic (same
source → same output), so a cached result is identical to what each module
would have produced on its own.

``test_pacific_days.py`` does NOT use this helper: it must build to an
isolated directory with ``TZ=UTC`` to pin the timezone regression, which is
a genuinely different build and stays separate.
"""

import subprocess
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = SITE_ROOT / "_site"

_build_result = None


def build_site() -> subprocess.CompletedProcess:
    """Run ``bundle exec jekyll build`` once and return the cached result.

    Returns the ``CompletedProcess`` so callers can inspect ``.returncode``,
    ``.stdout``, and ``.stderr`` exactly as they did with a direct
    ``subprocess.run`` call.
    """
    global _build_result
    if _build_result is None:
        _build_result = subprocess.run(
            ["bundle", "exec", "jekyll", "build", "--destination", str(SITE_DIR)],
            capture_output=True,
            text=True,
            cwd=str(SITE_ROOT),
            timeout=300,
        )
    return _build_result

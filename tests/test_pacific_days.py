"""Regression guard: published editions must appear on their Pacific-Time day.

Background
----------
The edition pipeline assigns every post its Pacific calendar day as the
filename date (``_posts/2026-09-12-Evening.html``) and writes the frontmatter
``date:`` with a PT UTC offset (``date: 2026-09-12 19:01:33 -0700``).

Jekyll, however, derives both the post *permalink* (``/news/:year/:month/:day/``)
and every Liquid ``| date:`` filter output from the frontmatter instant
converted to the **build host's** local timezone — unless ``timezone:`` is set
in ``_config.yml``. GitHub Pages builds on a UTC host, so with no
``timezone:`` the Evening edition (published 17:00 PT = 00:00 UTC the *next*
day) was presented under the following day's date, in the URL and in the
home-page day heading. That is the long-standing "evening edition is published
on the future day" bug.

These tests pin the property the reader actually sees: an edition's published
day is its Pacific day. They build with ``TZ=UTC`` in the environment — the
same shape as the GitHub Pages runner — so a build host outside Pacific time
can never silently reintroduce the bug. Setting ``TZ`` in the test is the
point: asserting a PT-day property while building in PT would pass even with
``timezone:`` missing from ``_config.yml``.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = PROJECT_ROOT / "_posts"
CONFIG_FILE = PROJECT_ROOT / "_config.yml"
PIPELINE_CONFIG = PROJECT_ROOT / "config.json"

# An isolated build directory so no other test class (several of which rebuild
# the shared _site/) can clobber the artifact we assert against.
_BUILD_DIR: Path | None = None


def _frontmatter_date(post_path: Path) -> datetime:
    """Return the timezone-aware frontmatter ``date:`` of a post."""
    text = post_path.read_text(encoding="utf-8")
    match = re.search(r"^date:\s*(.+)$", text, re.MULTILINE)
    if not match:
        raise AssertionError(f"{post_path.name} has no frontmatter date")
    return datetime.strptime(match.group(1).strip(), "%Y-%m-%d %H:%M:%S %z")


def _site_config() -> dict:
    with CONFIG_FILE.open() as handle:
        return yaml.safe_load(handle) or {}


def _pipeline_timezone() -> str:
    import json

    with PIPELINE_CONFIG.open() as handle:
        return json.load(handle).get("timezone") or "America/Los_Angeles"


def _edition_of(post_path: Path) -> str:
    """Edition label from a post filename: 2026-07-15-Evening.html -> Evening."""
    return post_path.stem.split("-", 3)[3]


def _pacific_day_path(post_path: Path) -> str:
    """Site-relative URL path this post must publish under (Pacific day)."""
    date = _frontmatter_date(post_path)
    return "news/{}/{}/{}/{}".format(
        date.strftime("%Y"),
        date.strftime("%m"),
        date.strftime("%d"),
        _edition_of(post_path),
    )


class TestBuildTimezoneConfiguration(unittest.TestCase):
    """The build must declare the site's timezone, not inherit the host's."""

    def test_site_config_declares_timezone(self):
        timezone = _site_config().get("timezone")
        self.assertTrue(
            timezone,
            "_config.yml has no `timezone:` key, so Jekyll interprets every "
            "post's frontmatter date in the build host's local timezone. On a "
            "UTC host that moves PT-evening editions to the next day.",
        )

    def test_site_timezone_matches_pipeline_timezone(self):
        """The site and the pipeline must agree on what 'today' means.

        Two independent timezone settings drift silently: the pipeline would
        date posts in one zone while Jekyll publishes them in another, which
        is the same class of bug this module exists to prevent.
        """
        self.assertEqual(_site_config().get("timezone"), _pipeline_timezone())

    def test_run_edition_pins_build_timezone(self):
        """run_edition.sh must not depend on the builder's ambient TZ.

        Belt-and-braces with the _config.yml setting: the shell that drives
        the build pins TZ so a host with no system timezone (or an unexpected
        one) still produces Pacific days.
        """
        script = (PROJECT_ROOT / "run_edition.sh").read_text(encoding="utf-8")
        self.assertRegex(
            script,
            r"^export TZ=|\bTZ=\"?\$TIMEZONE\"?",
            "run_edition.sh does not pin TZ for the Jekyll build",
        )


class TestPublishedDaysArePacificDays(unittest.TestCase):
    """Build on a UTC host and assert editions publish under their PT day."""

    @classmethod
    def setUpClass(cls):
        global _BUILD_DIR
        _BUILD_DIR = Path(tempfile.mkdtemp(prefix="jekyll-tz-build-"))
        env = dict(os.environ)
        # Mimic the GitHub Pages runner: a build host in UTC, not Pacific.
        env["TZ"] = "UTC"
        cls.build_result = subprocess.run(
            ["bundle", "exec", "jekyll", "build", "--destination", str(_BUILD_DIR)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )

    @classmethod
    def tearDownClass(cls):
        if _BUILD_DIR:
            shutil.rmtree(_BUILD_DIR, ignore_errors=True)

    def test_jekyll_build_succeeds(self):
        self.assertEqual(
            self.build_result.returncode,
            0,
            f"Jekyll build failed: {self.build_result.stderr[-500:]}",
        )

    def test_every_edition_publishes_under_its_pacific_day(self):
        """Each post's live URL day must be the PT day the pipeline chose.

        This is the user-visible contract: the site's edition day is the
        Pacific day, regardless of where the build ran.
        """
        posts = sorted(POSTS_DIR.glob("*.html"))
        self.assertGreater(len(posts), 100, "expected a populated _posts/ dir")

        missing = []
        for post in posts:
            expected = _BUILD_DIR / _pacific_day_path(post) / "index.html"
            if not expected.exists():
                missing.append(f"{post.name} -> /{_pacific_day_path(post)}/")

        self.assertEqual(
            missing,
            [],
            "editions not published on their Pacific day:\n" + "\n".join(missing[:10]),
        )

    def test_no_day_pages_outside_pacific_days(self):
        """No edition may be presented on a day no edition belongs to.

        The forward shift this module guards against is cumulative: every
        evening moves one day later, so the last evening in a run of days
        creates a whole extra day directory (2026-09-12's Evening appeared
        under a 2026-09-13 day that no edition belongs to). Asserting the
        build's day directories are a subset of the posts' Pacific days
        catches that, and — unlike asserting a specific late-day path is
        absent — does not misfire on the day the *next* edition legitimately
        occupies.
        """
        pacific_days = {}
        for post in POSTS_DIR.glob("*.html"):
            date = _frontmatter_date(post)
            pacific_days.setdefault(date.strftime("%Y/%m/%d"), set()).add(_edition_of(post))

        spurious = []
        for day_dir in sorted(_BUILD_DIR.glob("news/20??/??/??")):
            rel = day_dir.relative_to(_BUILD_DIR / "news").as_posix()
            if rel not in pacific_days:
                spurious.append(rel)
                continue
            for edition_dir in sorted(day_dir.iterdir()):
                if edition_dir.is_dir() and edition_dir.name not in pacific_days[rel]:
                    spurious.append(f"{rel}/{edition_dir.name}")

        self.assertEqual(
            spurious,
            [],
            "editions published on a day they do not belong to "
            "(the future-day shift):\n" + "\n".join(spurious[:10]),
        )

    def test_home_page_groups_latest_evening_under_its_pacific_day(self):
        """The home page day heading must be the edition's Pacific day."""
        home = _BUILD_DIR / "index.html"
        self.assertTrue(home.exists(), "home page missing from build output")
        content = home.read_text(encoding="utf-8")

        evening_posts = sorted(POSTS_DIR.glob("*-Evening.html"))
        self.assertTrue(evening_posts, "no Evening posts found")
        latest = evening_posts[-1]
        date = _frontmatter_date(latest)
        expected_href = "news/{}/{}/{}/".format(
            date.strftime("%Y"), date.strftime("%m"), date.strftime("%d")
        )

        # The newest day group must link to the newest edition's PT day.
        first_group = content.split('class="day-group"', 1)[1]
        heading_href = re.search(r'href="[^"]*/(' + re.escape(expected_href) + r')"', first_group)
        self.assertIsNotNone(
            heading_href,
            f"newest day group does not link to {expected_href} "
            f"(the PT day of {latest.name})",
        )


def _utc():
    from datetime import timezone

    return timezone.utc


if __name__ == "__main__":
    unittest.main()

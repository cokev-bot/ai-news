"""Tests for the public /source-status/ page (tools/build_source_status.py)
and the feed-health recording that feeds it.

Covers:
  - collect_feeds() over both sections.json formats, preserving authored order
  - last_item_times() / build_rows() joining state + health into rows
  - compute_status() classification for every status in the vocabulary
  - humanize_age() / parse_iso() edge cases (bad input, naive, future)
  - render_page() output shape (front matter, table, counts, escaping)
  - the page is written atomically and never raises on missing state files
  - generate_news.record_feed_health() merge semantics (success resets the
    streak, failure increments it) and its atomic write
  - fetch_feed(health_sink=...) records exactly one outcome per attempt
"""

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from build_source_status import (  # noqa: E402
    MAX_AGE_DAYS_DEFAULT,
    OUTPUT_FILE,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_OK,
    STATUS_QUIET,
    STATUS_STALE_CHECK,
    STATUS_UNKNOWN,
    build,
    build_rows,
    collect_feeds,
    compute_status,
    humanize_age,
    last_item_times,
    load_json,
    parse_iso,
    render_page,
    summarize,
)

from generate_news import (  # noqa: E402
    _load_feed_health,
    fetch_feed,
    record_feed_health,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def iso(delta: timedelta) -> str:
    """ISO timestamp *delta* before NOW."""
    return (NOW - delta).isoformat()


def make_site_root(tmp_path: Path, *, sections=None, state=None, health=None, config=None) -> Path:
    """Create a minimal site root for build() to operate on."""
    site_root = tmp_path / "site"
    site_root.mkdir(parents=True, exist_ok=True)
    if sections is None:
        sections = {
            "source_urls": {"FeedA": "https://a.example.com"},
            "sections": [
                {
                    "title": "News",
                    "subsections": [
                        {"title": "SubA", "feeds": {"FeedA": "https://a.example.com/rss"}},
                    ],
                },
                {
                    "title": "Labs",
                    "subsections": [
                        {
                            "title": "SubB",
                            "feeds": {"FeedB": "https://b.example.com/rss"},
                            "feeds_alts": {"FeedB": ["https://alt.example.com/rss"]},
                        },
                    ],
                },
            ],
        }
    (site_root / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    if config is not None:
        (site_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if state is not None:
        (site_root / ".news_state.json").write_text(json.dumps(state), encoding="utf-8")
    if health is not None:
        (site_root / ".feed_health.json").write_text(json.dumps(health), encoding="utf-8")
    return site_root


# ---------------------------------------------------------------------------
# collect_feeds
# ---------------------------------------------------------------------------

class TestCollectFeeds(unittest.TestCase):

    def test_object_format_lists_every_feed_in_order(self):
        data = {
            "source_urls": {"A": "https://a"},
            "sections": [
                {"title": "One", "subsections": [
                    {"title": "S1", "feeds": {"A": "u/a", "B": "u/b"}},
                ]},
                {"title": "Two", "subsections": [
                    {"title": "S2", "feeds": {"C": "u/c"}},
                ]},
            ],
        }
        feeds = collect_feeds(data)
        self.assertEqual([f["name"] for f in feeds], ["A", "B", "C"])
        self.assertEqual([f["section"] for f in feeds], ["One", "One", "Two"])
        self.assertEqual(feeds[0]["homepage"], "https://a")
        self.assertEqual(feeds[1]["homepage"], "")

    def test_legacy_flat_list_format_still_works(self):
        """Old sections.json shape has no source_urls and yields no homepages."""
        data = [
            {"title": "One", "subsections": [{"title": "S1", "feeds": {"A": "u/a"}}]},
        ]
        feeds = collect_feeds(data)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["name"], "A")
        self.assertEqual(feeds[0]["homepage"], "")

    def test_feeds_alts_recorded_as_fallback_count(self):
        data = {
            "sections": [
                {"title": "T", "subsections": [
                    {"title": "S", "feeds": {"A": "u/a"},
                     "feeds_alts": {"A": ["u/a2", "u/a3"]}},
                ]},
            ],
        }
        feeds = collect_feeds(data)
        self.assertEqual(feeds[0]["fallbacks"], ["u/a2", "u/a3"])

    def test_garbage_input_returns_empty(self):
        self.assertEqual(collect_feeds(None), [])
        self.assertEqual(collect_feeds("nonsense"), [])
        self.assertEqual(collect_feeds({"sections": "not a list"}), [])
        self.assertEqual(collect_feeds([{"no_subsections": True}]), [])

    def test_real_sections_json_has_34_feeds(self):
        """Regression pin: the live config defines 34 feeds, each renderable."""
        real = json.loads((PROJECT_ROOT / "sections.json").read_text(encoding="utf-8"))
        feeds = collect_feeds(real)
        self.assertEqual(len(feeds), 34)
        for feed in feeds:
            self.assertTrue(feed["name"], "every feed must have a name")
            self.assertTrue(feed["url"], f"{feed['name']} must have a url")


# ---------------------------------------------------------------------------
# parse_iso / humanize_age
# ---------------------------------------------------------------------------

class TestTimeHelpers(unittest.TestCase):

    def test_parse_naive_datetime_is_rejected(self):
        """Naive timestamps must not be compared against an aware now."""
        self.assertIsNone(parse_iso("2026-09-13T00:00:00"))

    def test_parse_zulu_suffix(self):
        dt = parse_iso("2026-09-13T00:00:00Z")
        self.assertEqual(dt, datetime(2026, 9, 13, tzinfo=timezone.utc))

    def test_parse_invalid_and_empty(self):
        for bad in (None, "", "   ", "not-a-date", 42, {"x": 1}):
            self.assertIsNone(parse_iso(bad), f"{bad!r} should not parse")

    def test_humanize_missing_is_never(self):
        age, absolute = humanize_age(None, NOW)
        self.assertEqual(age, "never")
        self.assertEqual(absolute, "")

    def test_humanize_minute_hour_day_buckets(self):
        self.assertEqual(humanize_age(iso(timedelta(seconds=10)), NOW)[0], "just now")
        self.assertEqual(humanize_age(iso(timedelta(minutes=5)), NOW)[0], "5m ago")
        self.assertEqual(humanize_age(iso(timedelta(hours=7)), NOW)[0], "7h ago")
        self.assertEqual(humanize_age(iso(timedelta(days=6)), NOW)[0], "6d ago")
        self.assertEqual(humanize_age(iso(timedelta(days=90)), NOW)[0], "3mo ago")

    def test_humanize_future_clamps_to_just_now(self):
        """Clock skew must not render a negative age."""
        age, _ = humanize_age(iso(timedelta(hours=-3)), NOW)
        self.assertEqual(age, "just now")

    def test_humanize_absolute_is_utc_iso(self):
        _, absolute = humanize_age(iso(timedelta(hours=1)), NOW)
        self.assertTrue(absolute.endswith("+00:00"))


# ---------------------------------------------------------------------------
# last_item_times
# ---------------------------------------------------------------------------

class TestLastItemTimes(unittest.TestCase):

    def test_takes_latest_seen_at_per_feed(self):
        seen = {
            "l1": {"feed": "A", "seen_at": iso(timedelta(days=5))},
            "l2": {"feed": "A", "seen_at": iso(timedelta(hours=2))},
            "l3": {"feed": "B", "seen_at": iso(timedelta(days=3))},
        }
        latest, counts = last_item_times(seen, now=NOW, window_days=7)
        self.assertEqual(latest["A"], NOW - timedelta(hours=2))
        self.assertEqual(latest["B"], NOW - timedelta(days=3))
        self.assertEqual(counts["A"], 2)
        self.assertEqual(counts["B"], 1)

    def test_window_excludes_older_items_from_count_but_not_from_latest(self):
        seen = {
            "l1": {"feed": "A", "seen_at": iso(timedelta(days=30))},
            "l2": {"feed": "B", "seen_at": iso(timedelta(days=1))},
        }
        latest, counts = last_item_times(seen, now=NOW, window_days=7)
        self.assertIn("A", latest, "old items still date the source")
        self.assertEqual(counts["A"], 0, "but they are outside the window count")
        self.assertEqual(counts["B"], 1)

    def test_entries_without_feed_or_seen_at_are_ignored(self):
        seen = {
            "l1": {"seen_at": iso(timedelta(hours=1))},          # no feed
            "l2": {"feed": "A"},                                  # no seen_at
            "l3": {"feed": "A", "seen_at": "garbage"},           # unparsable
            "l4": "not-a-dict",
        }
        latest, counts = last_item_times(seen, now=NOW, window_days=7)
        self.assertEqual(latest, {})
        self.assertEqual(sum(counts.values()), 0)

    def test_empty_and_none_input(self):
        self.assertEqual(last_item_times({}, now=NOW, window_days=7), ({}, {}))
        self.assertEqual(last_item_times(None, now=NOW, window_days=7), ({}, {}))


# ---------------------------------------------------------------------------
# compute_status
# ---------------------------------------------------------------------------

class TestComputeStatus(unittest.TestCase):

    def _status(self, *, last_item=None, last_success=None, failures=0):
        return compute_status(
            last_item=last_item,
            last_success=last_success,
            failures=failures,
            now=NOW,
            max_age_days=MAX_AGE_DAYS_DEFAULT,
        )

    def test_never_fetched_is_unknown(self):
        self.assertEqual(self._status(), STATUS_UNKNOWN)

    def test_three_failures_is_failing(self):
        self.assertEqual(
            self._status(last_success=NOW - timedelta(hours=1), failures=3),
            STATUS_FAILING,
        )

    def test_failures_beat_a_recent_success_stamp(self):
        """A stale last_success with an active failure streak is not 'ok'."""
        self.assertEqual(
            self._status(last_success=NOW - timedelta(hours=1), failures=1),
            STATUS_DEGRADED,
        )

    def test_fresh_fetch_with_recent_item_is_ok(self):
        self.assertEqual(
            self._status(last_item=NOW - timedelta(days=1),
                         last_success=NOW - timedelta(hours=4)),
            STATUS_OK,
        )

    def test_fresh_fetch_with_no_items_is_quiet(self):
        self.assertEqual(
            self._status(last_success=NOW - timedelta(hours=4)), STATUS_QUIET)

    def test_fresh_fetch_with_aged_out_item_is_quiet(self):
        self.assertEqual(
            self._status(last_item=NOW - timedelta(days=30),
                         last_success=NOW - timedelta(hours=4)),
            STATUS_QUIET,
        )

    def test_no_failures_but_stale_check_is_flagged(self):
        """A 'success' from months ago must not read as healthy."""
        self.assertEqual(
            self._status(last_item=NOW - timedelta(days=1),
                         last_success=NOW - timedelta(days=90)),
            STATUS_STALE_CHECK,
        )

    def test_failing_takes_precedence_over_stale_check(self):
        self.assertEqual(
            self._status(last_success=NOW - timedelta(days=90), failures=5),
            STATUS_FAILING,
        )


# ---------------------------------------------------------------------------
# build_rows / summarize
# ---------------------------------------------------------------------------

class TestBuildRows(unittest.TestCase):

    def _feeds(self):
        return [
            {"name": "A", "url": "u/a", "homepage": "https://a", "section": "News",
             "subsection": "SubA", "fallbacks": []},
            {"name": "B", "url": "u/b", "homepage": "", "section": "News",
             "subsection": "SubA", "fallbacks": ["x"]},
            {"name": "C", "url": "u/c", "homepage": "", "section": "Labs",
             "subsection": "SubB", "fallbacks": []},
        ]

    def test_joins_state_and_health(self):
        seen = {"l1": {"feed": "A", "seen_at": iso(timedelta(days=1))}}
        health = {"A": {"last_success": iso(timedelta(hours=2)), "consecutive_failures": 0},
                  "B": {"last_success": iso(timedelta(hours=2)), "consecutive_failures": 2}}
        rows = build_rows(self._feeds(), seen, health, now=NOW)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["A"]["status"], STATUS_OK)
        self.assertEqual(by_name["A"]["item_count"], 1)
        self.assertEqual(by_name["B"]["status"], STATUS_DEGRADED)
        self.assertEqual(by_name["C"]["status"], STATUS_UNKNOWN)
        self.assertIsNone(by_name["C"]["last_item"])

    def test_every_feed_appears_even_with_no_state_at_all(self):
        rows = build_rows(self._feeds(), {}, {}, now=NOW)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == STATUS_UNKNOWN for r in rows))

    def test_row_order_matches_sections_json_order(self):
        rows = build_rows(self._feeds(), {}, {}, now=NOW)
        self.assertEqual([r["name"] for r in rows], ["A", "B", "C"])

    def test_garbage_health_entry_is_treated_as_unknown(self):
        rows = build_rows(self._feeds(), {}, {"A": "not-a-dict"}, now=NOW)
        self.assertEqual(rows[0]["status"], STATUS_UNKNOWN)

    def test_non_integer_failure_count_coerced(self):
        health = {"A": {"last_success": iso(timedelta(hours=2)), "consecutive_failures": "4"}}
        rows = build_rows(self._feeds(), {}, health, now=NOW)
        self.assertEqual(rows[0]["failures"], 4)
        self.assertEqual(rows[0]["status"], STATUS_FAILING)

    def test_summary_counts(self):
        seen = {"l1": {"feed": "A", "seen_at": iso(timedelta(days=1))}}
        health = {"A": {"last_success": iso(timedelta(hours=2)), "consecutive_failures": 0},
                  "B": {"last_success": iso(timedelta(hours=2)), "consecutive_failures": 0}}
        rows = build_rows(self._feeds(), seen, health, now=NOW)
        summary = summarize(rows)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["delivering"], 1)
        self.assertEqual(summary["quiet"], 1)
        self.assertEqual(summary["unknown"], 1)


# ---------------------------------------------------------------------------
# render_page
# ---------------------------------------------------------------------------

class TestRenderPage(unittest.TestCase):

    def _rows(self):
        return build_rows(
            [{"name": "A", "url": "u/a", "homepage": "https://a", "section": "News",
              "subsection": "SubA", "fallbacks": []}],
            {"l1": {"feed": "A", "seen_at": iso(timedelta(days=1))}},
            {"A": {"last_success": iso(timedelta(hours=2)), "consecutive_failures": 0}},
            now=NOW,
        )

    def test_front_matter_and_permalink(self):
        html = render_page(self._rows(), now=NOW)
        self.assertTrue(html.startswith("---\n"))
        self.assertIn("layout: page", html)
        self.assertIn("permalink: /source-status/", html)
        self.assertIn("title: Source Status", html)

    def test_no_duplicate_h1_from_body(self):
        """The theme renders page.title as <h1>; adding one in the body would
        produce two top-level headings on the same page."""
        html = render_page(self._rows(), now=NOW)
        body = html.split("---\n", 2)[2]
        self.assertNotIn("<h1", body.lower())
        self.assertNotIn("Source Status</h1>", body)

    def test_has_all_six_columns(self):
        html = render_page(self._rows(), now=NOW)
        for header in ("Source", "Section", "Last new story",
                       "Last successful fetch", "Items", "Status"):
            self.assertIn(header, html, f"missing column header {header!r}")
        self.assertIn("(UTC)", html, "columns must state the timezone")
        self.assertIn("<table", html)
        self.assertIn("</table>", html)

    def test_timestamps_render_as_absolute_utc(self):
        """Times must be absolute, not relative.

        The page is generated once and then sits static, so a relative label
        like "2m ago" freezes: read an hour later it still says "2m ago" and is
        simply wrong. Pin the absolute form so this cannot regress.
        """
        html = render_page(self._rows(), now=NOW)
        self.assertRegex(html, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
        self.assertNotIn("ago<", html,
                         "relative ages must not be the visible cell text")

    def test_relative_age_survives_in_the_tooltip(self):
        """The convenience of a relative age is kept, not discarded."""
        html = render_page(self._rows(), now=NOW)
        self.assertIn("title=", html)
        self.assertIn("at build time", html)

    def test_never_rendered_for_unknown_sources(self):
        rows = build_rows(
            [{"name": "A", "url": "u/a", "homepage": "", "section": "News",
              "subsection": "SubA", "fallbacks": []}], {}, {}, now=NOW)
        html = render_page(rows, now=NOW)
        self.assertIn("ss-never", html)

    def test_absolute_time_is_stable_regardless_of_when_read(self):
        """The rendered data cells must not depend on the reader's clock.

        Rendering the same input data with "now" an hour later must produce the
        identical visible timestamps — which is exactly what a relative label
        fails to do. The page's own "updated ..." line is excluded: it is a
        build timestamp and correctly reflects when the page was generated.
        """
        from datetime import timedelta
        rows = self._rows()
        early = render_page(rows, now=NOW)
        late = render_page(rows, now=NOW + timedelta(hours=1))

        def cells(page: str) -> list[str]:
            body = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
            return re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", body)

        self.assertEqual(cells(early), cells(late))
        self.assertTrue(cells(early), "expected at least one timestamp cell")

    def test_summary_line_reports_live_counts(self):
        html = render_page(self._rows(), now=NOW)
        self.assertIn("1</strong> sources tracked", html)
        self.assertIn("1</strong> delivering stories", html)

    def test_source_names_and_urls_are_escaped(self):
        rows = build_rows(
            [{"name": 'Evil<script>"', "url": "u", "homepage": "",
              "section": "News", "subsection": "S", "fallbacks": []}],
            {}, {}, now=NOW)
        html = render_page(rows, now=NOW)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_homepage_link_rendered_when_known(self):
        html = render_page(self._rows(), now=NOW)
        self.assertIn('<a href="https://a">A</a>', html)

    def test_group_header_per_section(self):
        rows = build_rows(
            [{"name": "A", "url": "u/a", "homepage": "", "section": "News",
              "subsection": "S1", "fallbacks": []},
             {"name": "B", "url": "u/b", "homepage": "", "section": "Labs",
              "subsection": "S2", "fallbacks": []}],
            {}, {}, now=NOW)
        html = render_page(rows, now=NOW)
        self.assertEqual(html.count('class="ss-group"'), 2)
        self.assertIn(">News<", html)
        self.assertIn(">Labs<", html)

    def test_status_badges_use_status_specific_class(self):
        rows = build_rows(
            [{"name": "A", "url": "u/a", "homepage": "", "section": "N",
              "subsection": "S", "fallbacks": []}],
            {}, {"A": {"last_success": iso(timedelta(hours=1)), "consecutive_failures": 4}},
            now=NOW)
        html = render_page(rows, now=NOW)
        self.assertIn("ss-badge-failing", html)
        self.assertIn(">Failing<", html)

    def test_last_error_surfaced_in_tooltip(self):
        """A failure count alone isn't actionable; the reason must be visible."""
        rows = build_rows(
            [{"name": "A", "url": "u/a", "homepage": "", "section": "N",
              "subsection": "S", "fallbacks": []}],
            {},
            {"A": {"last_success": iso(timedelta(hours=1)), "consecutive_failures": 4,
                   "last_error": "all 4 URL(s) failed (unparseable XML)"}},
            now=NOW)
        html = render_page(rows, now=NOW)
        self.assertIn("unparseable XML", html)

    def test_error_text_is_escaped_in_tooltip(self):
        rows = build_rows(
            [{"name": "A", "url": "u/a", "homepage": "", "section": "N",
              "subsection": "S", "fallbacks": []}],
            {},
            {"A": {"last_success": iso(timedelta(hours=1)), "consecutive_failures": 4,
                   "last_error": '"><script>alert(1)</script>'}},
            now=NOW)
        html = render_page(rows, now=NOW)
        self.assertNotIn("<script>alert(1)</script>", html)


# ---------------------------------------------------------------------------
# build() end-to-end
# ---------------------------------------------------------------------------

class TestBuildEndToEnd(unittest.TestCase):

    def test_writes_page_and_covers_every_feed(self):
        with tempfile.TemporaryDirectory() as td:
            site_root = make_site_root(
                Path(td),
                state={"seen_links": {"l1": {"feed": "FeedA",
                                             "seen_at": iso(timedelta(days=1))}}},
                health={"FeedA": {"last_success": iso(timedelta(hours=2)),
                                  "consecutive_failures": 0},
                        "FeedB": {"last_success": iso(timedelta(hours=2)),
                                  "consecutive_failures": 5}},
                config={"tuning": {"max_age_days": 7}},
            )
            out = build(site_root, now=NOW)
            self.assertEqual(out.name, OUTPUT_FILE)
            html = out.read_text(encoding="utf-8")
            self.assertEqual(html.count('class="ss-row'), 2)
            self.assertIn("FeedA", html)
            self.assertIn("FeedB", html)

    def test_no_state_files_yields_page_not_exception(self):
        """A brand-new checkout has neither state file; the page must still build."""
        with tempfile.TemporaryDirectory() as td:
            site_root = make_site_root(Path(td))
            out = build(site_root, now=NOW)
            html = out.read_text(encoding="utf-8")
            self.assertEqual(html.count('class="ss-row'), 2)
            self.assertIn(">never<", html)

    def test_corrupt_state_files_are_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            site_root = make_site_root(Path(td))
            (site_root / ".news_state.json").write_text("{{{ not json", encoding="utf-8")
            (site_root / ".feed_health.json").write_text("also not json", encoding="utf-8")
            out = build(site_root, now=NOW)
            self.assertIn('class="ss-row', out.read_text(encoding="utf-8"))

    def test_missing_sections_json_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                build(Path(td), now=NOW)

    def test_write_is_atomic_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            site_root = make_site_root(Path(td))
            build(site_root, now=NOW)
            self.assertFalse((site_root / "source-status.html.tmp").exists())
            self.assertTrue((site_root / OUTPUT_FILE).exists())

    def test_max_age_days_read_from_config(self):
        with tempfile.TemporaryDirectory() as td:
            site_root = make_site_root(
                Path(td),
                state={"seen_links": {"l1": {"feed": "FeedA",
                                             "seen_at": iso(timedelta(days=10))}}},
                health={"FeedA": {"last_success": iso(timedelta(hours=1)),
                                  "consecutive_failures": 0}},
                config={"tuning": {"max_age_days": 3}},
            )
            html = build(site_root, now=NOW).read_text(encoding="utf-8")
            # 10-day-old item is outside a 3-day window → quiet, not ok
            self.assertIn("ss-badge-quiet", html)
            self.assertIn("Items (3d)", html)

    def test_invalid_max_age_days_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as td:
            site_root = make_site_root(Path(td), config={"tuning": {"max_age_days": "seven"}})
            html = build(site_root, now=NOW).read_text(encoding="utf-8")
            self.assertIn(f"Items ({MAX_AGE_DAYS_DEFAULT}d)", html)

    def test_load_json_handles_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.json"
            self.assertEqual(load_json(p, {"default": True}), {"default": True})
            p.write_text("{{{", encoding="utf-8")
            self.assertEqual(load_json(p, []), [])


# ---------------------------------------------------------------------------
# record_feed_health (generate_news)
# ---------------------------------------------------------------------------

class TestRecordFeedHealth(unittest.TestCase):

    def test_success_stamps_last_success_and_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            health = {"A": {"url": "u", "consecutive_failures": 4,
                            "last_success": None, "last_failure": "old",
                            "last_error": "boom"}}
            (root / ".feed_health.json").write_text(json.dumps(health), encoding="utf-8")
            record_feed_health(root, [{"name": "A", "url": "u", "ok": True, "error": None}])
            entry = _load_feed_health(root)["A"]
            self.assertEqual(entry["consecutive_failures"], 0)
            self.assertIsNone(entry["last_error"])
            self.assertIsNotNone(entry["last_success"])

    def test_failure_increments_streak_and_records_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record_feed_health(root, [{"name": "A", "url": "u", "ok": False, "error": "timeout"}])
            entry = _load_feed_health(root)["A"]
            self.assertEqual(entry["consecutive_failures"], 1)
            self.assertEqual(entry["last_error"], "timeout")
            self.assertIsNotNone(entry["last_failure"])
            self.assertIsNone(entry["last_success"])

    def test_repeated_failures_accumulate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(3):
                record_feed_health(root, [{"name": "A", "url": "u", "ok": False,
                                           "error": "down"}])
            self.assertEqual(_load_feed_health(root)["A"]["consecutive_failures"], 3)

    def test_no_results_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record_feed_health(root, [])
            self.assertEqual(_load_feed_health(root), {})

    def test_unrelated_feeds_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            health = {"Other": {"url": "u", "consecutive_failures": 0,
                                "last_success": "2026-01-01T00:00:00+00:00",
                                "last_failure": None, "last_error": None}}
            (root / ".feed_health.json").write_text(json.dumps(health), encoding="utf-8")
            record_feed_health(root, [{"name": "A", "url": "u", "ok": True, "error": None}])
            loaded = _load_feed_health(root)
            self.assertIn("Other", loaded)
            self.assertIn("A", loaded)

    def test_write_leaves_no_tmp_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record_feed_health(root, [{"name": "A", "url": "u", "ok": True, "error": None}])
            self.assertFalse((root / ".feed_health.json.tmp").exists())

    def test_corrupt_existing_health_file_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".feed_health.json").write_text("{{{", encoding="utf-8")
            record_feed_health(root, [{"name": "A", "url": "u", "ok": True, "error": None}])
            self.assertEqual(_load_feed_health(root)["A"]["consecutive_failures"], 0)


# ---------------------------------------------------------------------------
# fetch_feed health_sink wiring
# ---------------------------------------------------------------------------

MINI_RSS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel><title>T</title>'
    b'<item><title>Hello</title><link>https://example.com/1</link>'
    b'<pubDate>DYNAMIC</pubDate></item></channel></rss>'
)


def _fresh_rss(days_ago: int = 0) -> bytes:
    """RSS body with a pubDate inside the age window (never ages out)."""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000")
    return MINI_RSS.replace(b"DYNAMIC", stamp.encode())


class TestFetchFeedHealthSink(unittest.TestCase):

    def test_successful_fetch_records_ok(self):
        sink: list[dict] = []
        with patch("generate_news._http_get_with_retry", return_value=_fresh_rss()):
            articles = fetch_feed("F", "https://example.com/rss", health_sink=sink)
        self.assertEqual(len(articles), 1)
        self.assertEqual(len(sink), 1)
        self.assertTrue(sink[0]["ok"])
        self.assertEqual(sink[0]["name"], "F")
        self.assertEqual(sink[0]["url"], "https://example.com/rss")
        self.assertIsNone(sink[0]["error"])

    def test_all_urls_failing_records_one_failure(self):
        sink: list[dict] = []
        with patch("generate_news._http_get_with_retry", return_value=None):
            articles = fetch_feed("F", "https://example.com/rss",
                                  fallbacks=["https://alt/rss"], health_sink=sink)
        self.assertEqual(articles, [])
        self.assertEqual(len(sink), 1, "one outcome per feed, not per URL")
        self.assertFalse(sink[0]["ok"])
        self.assertIn("2 URL(s) failed", sink[0]["error"])

    def test_parse_error_records_failure(self):
        sink: list[dict] = []
        with patch("generate_news._http_get_with_retry", return_value=b"<html>" + b"x" * 200):
            articles = fetch_feed("F", "https://example.com/rss", health_sink=sink)
        self.assertEqual(articles, [])
        self.assertEqual(len(sink), 1)
        self.assertFalse(sink[0]["ok"])
        self.assertIn("parse error", sink[0]["error"])

    def test_healthy_but_empty_feed_still_counts_as_reachable(self):
        """A feed that parses fine but has nothing new is still 'fetched OK'."""
        sink: list[dict] = []
        with patch("generate_news._http_get_with_retry", return_value=_fresh_rss(days_ago=30)):
            articles = fetch_feed("F", "https://example.com/rss", health_sink=sink)
        self.assertEqual(articles, [], "aged-out item is dropped")
        self.assertTrue(sink[0]["ok"], "but the fetch itself succeeded")

    def test_no_sink_is_backward_compatible(self):
        with patch("generate_news._http_get_with_retry", return_value=_fresh_rss()):
            articles = fetch_feed("F", "https://example.com/rss")
        self.assertEqual(len(articles), 1)

    def test_sink_records_success_on_fallback_url(self):
        sink: list[dict] = []
        responses = [None, _fresh_rss()]
        with patch("generate_news._http_get_with_retry", side_effect=responses), \
             patch("generate_news.time.sleep"):
            articles = fetch_feed("F", "https://example.com/rss",
                                  fallbacks=["https://alt/rss"], health_sink=sink)
        self.assertEqual(len(articles), 1)
        self.assertTrue(sink[0]["ok"])


class TestFetchAllFeedsHealthSink(unittest.TestCase):
    """fetch_all_feeds must thread the sink into every parallel worker."""

    def test_sink_collects_one_entry_per_feed(self):
        from generate_news import fetch_all_feeds

        sections = [
            {"title": "S", "subsections": [
                {"title": "Sub", "feeds": {"A": "u/a", "B": "u/b", "C": "u/c"}},
            ]},
        ]
        sink: list[dict] = []

        def fake_fetch_feed(name, url, fallbacks=None, **kwargs):
            s = kwargs.get("health_sink")
            if s is not None:
                s.append({"name": name, "url": url, "ok": name != "B",
                          "error": None if name != "B" else "boom"})
            return []

        with patch("generate_news.fetch_feed", side_effect=fake_fetch_feed):
            fetch_all_feeds(sections, health_sink=sink)

        self.assertEqual(len(sink), 3)
        self.assertEqual(sorted(e["name"] for e in sink), ["A", "B", "C"])
        self.assertFalse([e for e in sink if e["name"] == "B"][0]["ok"])


class TestGeneratePostRecordsHealth(unittest.TestCase):
    """generate_post() must persist feed health from its own fetch pass.

    This is the seam that keeps the public source-status page's "last
    successful fetch" column live: the edition cron fetches every feed three
    times a day, so health recorded there stays current even when the
    standalone tools/check_feeds.py monitor is not scheduled.
    """

    def _make_site_root(self, tmp_path: Path) -> Path:
        from generate_news import DEFAULT_CONFIG

        site_root = tmp_path / "site"
        site_root.mkdir()
        (site_root / "config.json").write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
        (site_root / "sections.json").write_text(json.dumps({
            "source_urls": {"FeedA": "https://a"},
            "sections": [
                {"title": "News", "subsections": [
                    {"title": "SubA", "feeds": {"FeedA": "https://a/rss",
                                                "FeedB": "https://b/rss"}},
                ]},
            ],
        }), encoding="utf-8")
        (site_root / "summary_prompt.txt").write_text("Summarize: ", encoding="utf-8")
        (site_root / "_posts").mkdir()
        return site_root

    def _run(self, site_root: Path):
        from generate_news import generate_post

        def fake_fetch_all_feeds(sections, **kwargs):
            sink = kwargs.get("health_sink")
            if sink is not None:
                sink.append({"name": "FeedA", "url": "https://a/rss",
                             "ok": True, "error": None})
                sink.append({"name": "FeedB", "url": "https://b/rss",
                             "ok": False, "error": "all 1 URL(s) failed"})
            return {"SubA": [("FeedA", [{
                "title": "A story",
                "link": "https://a/story",
                "source": "FeedA",
                "description": "d",
                "pub": "",
                "pub_dt": None,
            }])]}

        with patch("generate_news.fetch_all_feeds", side_effect=fake_fetch_all_feeds), \
             patch("generate_news._query_ollama", return_value="Summary."), \
             patch("generate_news.generate_edition_audio", return_value={}), \
             patch("generate_news.generate_og_image_for_edition", return_value=None):
            return generate_post("2026-09-13-morning", site_root)

    def test_health_file_written_with_both_feeds(self):
        with tempfile.TemporaryDirectory() as td:
            site_root = self._make_site_root(Path(td))
            self.assertTrue(self._run(site_root))
            health = _load_feed_health(site_root)
            self.assertIn("FeedA", health)
            self.assertIn("FeedB", health)
            self.assertEqual(health["FeedA"]["consecutive_failures"], 0)
            self.assertIsNotNone(health["FeedA"]["last_success"])
            self.assertEqual(health["FeedB"]["consecutive_failures"], 1)
            self.assertEqual(health["FeedB"]["last_error"], "all 1 URL(s) failed")

    def test_failure_to_record_health_does_not_break_the_edition(self):
        """Health bookkeeping is best-effort; an edition must still publish."""
        with tempfile.TemporaryDirectory() as td:
            site_root = self._make_site_root(Path(td))
            with patch("generate_news.record_feed_health",
                       side_effect=OSError("disk full")):
                self.assertTrue(self._run(site_root))
            self.assertTrue(list((site_root / "_posts").glob("*.html")))


if __name__ == "__main__":
    unittest.main()

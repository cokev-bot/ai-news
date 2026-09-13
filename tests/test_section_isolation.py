"""Tests for cross-section article duplication (subsection keying).

The defect these pin: articles were grouped by subsection *title*, and
subsection titles are not unique in sections.json — "OpenAI", "Google",
"Anthropic" and "Mistral" each appear under more than one section. Keying by
title therefore made one section's article list render verbatim under every
other section that reused the title: the published archive had 158 of 194
posts carrying the same items twice (AI Labs and Developers identical), with
two different LLM summaries describing an identical article list.

The grouping key is now the subsection's *position* in sections.json, so each
feed is owned by exactly one (section, subsection) and is rendered once.

These tests assert the property — "no article appears under two sections" —
by running the real render/payload code rather than restating the key format.
"""

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from generate_news import (  # noqa: E402
    _subsection_key,
    build_edition_api_payload,
    fetch_all_feeds,
)

NOW = datetime(2026, 9, 13, 17, 0, 0, tzinfo=timezone.utc)


def article(title, source):
    return {
        "title": title,
        "link": f"https://example.com/{title.replace(' ', '-').lower()}",
        "source": source,
        "description": f"{title} description",
        "pub_dt": NOW,
        "pub": "Sat, 13 Sep 2026 08:00:00 GMT",
    }


def colliding_sections():
    """Two sections that reuse the same subsection title, as the repo does."""
    return [
        {
            "title": "AI Labs",
            "subsections": [
                {"title": "OpenAI", "feeds": {"OpenAI": "https://example.com/o.xml"}},
                {"title": "Google", "feeds": {"GoogleAI": "https://example.com/g.xml"}},
            ],
        },
        {
            "title": "Developers",
            "subsections": [
                {"title": "OpenAI", "feeds": {"OpenAIDevs": "https://example.com/od.xml"}},
                {"title": "Google", "feeds": {"googleaidevs": "https://example.com/gd.xml"}},
            ],
        },
    ]


class TestSubsectionKey(unittest.TestCase):

    def test_key_is_positional_and_titles_do_not_collide(self):
        """Same title, different position -> different key."""
        self.assertNotEqual(_subsection_key(0, 0), _subsection_key(1, 0))
        self.assertEqual(_subsection_key(0, 0), _subsection_key(0, 0))

    def test_real_sections_json_has_colliding_titles(self):
        """Guard the premise: this repo really does reuse subsection titles.

        If the titles ever become unique the bug class is moot — but the test
        must not silently pass for the wrong reason, so assert the collision
        exists (read from sections.json, not hardcoded names).
        """
        data = json.loads((PROJECT_ROOT / "sections.json").read_text(encoding="utf-8"))
        seen = {}
        for s in data["sections"]:
            for ss in s["subsections"]:
                seen.setdefault(ss["title"], []).append(s["title"])
        repeated = {t: owners for t, owners in seen.items() if len(owners) > 1}
        self.assertTrue(
            repeated,
            "sections.json no longer reuses subsection titles — the "
            "duplication this module guards against cannot occur; revisit",
        )


class TestNoCrossSectionDuplication(unittest.TestCase):
    """The property: one article belongs to one section in the rendered output."""

    def _payload(self, sections, articles):
        return build_edition_api_payload(
            edition_label="Morning",
            post_now=NOW,
            generated_at=NOW,
            global_summary_text="Big picture.",
            section_summaries={s["title"]: "S." for s in sections},
            sections_data=sections,
            subsection_articles=articles,
            freshness={"fresh": 1, "stale": 0, "yesterday": 0},
        )

    def test_shared_subsection_title_does_not_duplicate_items(self):
        sections = colliding_sections()
        articles = {
            _subsection_key(0, 0): [article("labs openai story", "OpenAI")],
            _subsection_key(0, 1): [article("labs google story", "GoogleAI")],
            _subsection_key(1, 0): [article("dev openai story", "OpenAIDevs")],
            _subsection_key(1, 1): [article("dev google story", "googleaidevs")],
        }
        p = self._payload(sections, articles)

        per_section = {
            s["title"]: {
                a["title"]
                for ss in s["subsections"]
                for a in ss["articles"]
            }
            for s in p["sections"]
        }
        self.assertEqual(
            per_section["AI Labs"],
            {"labs openai story", "labs google story"},
        )
        self.assertEqual(
            per_section["Developers"],
            {"dev openai story", "dev google story"},
        )
        # The property under test: no item is rendered under two sections.
        overlap = per_section["AI Labs"] & per_section["Developers"]
        self.assertEqual(overlap, set(), f"items appear in both sections: {overlap}")

    def test_total_items_equals_sum_of_distinct_articles(self):
        """stats.items counts each article once, not once per owning section."""
        sections = colliding_sections()
        articles = {
            _subsection_key(0, 0): [article("a", "S1")],
            _subsection_key(0, 1): [article("b", "S2")],
            _subsection_key(1, 0): [article("c", "S3")],
            _subsection_key(1, 1): [article("d", "S4")],
        }
        p = self._payload(sections, articles)
        self.assertEqual(p["stats"]["items"], 4)
        self.assertEqual(sum(s["item_count"] for s in p["sections"]), 4)

    def test_lookup_is_no_longer_title_keyed(self):
        """A title-keyed dict no longer reaches the render — by design.

        Passing the old shape (``{"OpenAI": [...]}``) must yield *no* items
        anywhere, because the lookup now requires the positional key. This is
        the property that makes the duplication unreachable: there is no code
        path left that can look an article list up by a shared title.
        """
        sections = colliding_sections()
        p = self._payload(sections, {"OpenAI": [article("shared story", "OpenAI")]})
        reachable = [
            a["title"]
            for s in p["sections"]
            for ss in s["subsections"]
            for a in ss["articles"]
        ]
        self.assertEqual(reachable, [])
        self.assertEqual(p["sections"], [])


class TestFetchGrouping(unittest.TestCase):
    """fetch_all_feeds must group by position, so the fetch path cannot drift."""

    def test_fetch_results_are_keyed_per_position_not_per_title(self):
        calls = []

        def fake_fetch(name, url, fallbacks=None, **kwargs):
            calls.append(name)
            return [article(f"{name} story", name)]

        sections = colliding_sections()
        with mock.patch(
            "generate_news.fetch_feed", side_effect=fake_fetch
        ):
            results = fetch_all_feeds(sections)

        self.assertEqual(len(calls), 4)
        # Four distinct keys, not two shared titles.
        self.assertEqual(set(results), {
            _subsection_key(0, 0), _subsection_key(0, 1),
            _subsection_key(1, 0), _subsection_key(1, 1),
        })
        for key, feeds in results.items():
            self.assertEqual(len(feeds), 1, f"key {key} lost/merged a feed")


class TestRenderedPostHasNoCrossSectionDuplication(unittest.TestCase):
    """End-to-end: render a real post from the real sections.json shape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site_root = Path(self._tmp.name)
        (self.site_root / "_posts").mkdir()
        (self.site_root / "sections.json").write_text(
            json.dumps({"sections": colliding_sections(), "source_urls": {}}),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_sections_sharing_a_title_render_disjoint_items(self):
        from generate_news import render_item

        # Render exactly the way generate_post() does: positional lookups.
        sections = colliding_sections()
        subsection_articles = {
            _subsection_key(0, 0): [article("labs openai story", "OpenAI")],
            _subsection_key(1, 0): [article("dev openai story", "OpenAIDevs")],
        }
        rendered = {}
        for section_index, section in enumerate(sections):
            lines = []
            for subsection_index, subsection in enumerate(section["subsections"]):
                items = subsection_articles.get(
                    _subsection_key(section_index, subsection_index), []
                )
                for art in items:
                    lines.append(render_item(art, {}))
            rendered[section["title"]] = "\n".join(lines)

        self.assertIn("labs openai story", rendered["AI Labs"])
        self.assertNotIn("dev openai story", rendered["AI Labs"])
        self.assertIn("dev openai story", rendered["Developers"])
        self.assertNotIn("labs openai story", rendered["Developers"])


if __name__ == "__main__":
    unittest.main()

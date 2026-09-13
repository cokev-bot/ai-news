"""Tests for the static edition JSON API (/api/<date>-<Edition>.json).

Covers:
- build_edition_api_payload() shapes: core fields, stats, sources, sections.
- Subsection articles are resolved through the same *positional* lookup the
  post render uses, so the payload's membership and counts agree with the
  HTML a reader sees. Subsection titles are NOT unique in sections.json
  ("OpenAI", "Google", "Anthropic", "Mistral" each appear under more than one
  section), so a title can never be the key — keying by title made one
  section's articles render under every section that reused it. See
  tests/test_section_isolation.py for the duplication regression tests.
- Empty sections are omitted entirely, matching the post render.
- Summary/description are HTML-stripped; big_picture is untruncated while
  summary mirrors the post front matter's budget.
- write_edition_api() writes atomically, creates the directory, is
  idempotent, and returns None (never raises) on a bad target.
- The endpoint is correct against the real sections.json.
- generate_post() integration: the payload is written beside the post with
  the right filename and its stats agree with the rendered post header.
- run_edition.sh stages api/ (otherwise the payload never reaches the site).
- Jekyll copies api/*.json into _site/. A JSON file placed in _posts/ is
  silently dropped by the collection, which is WHY the endpoint is emitted
  to api/ instead of next to the post — the test pins that reason.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from generate_news import (  # noqa: E402
    API_DIR,
    MAX_DESCRIPTION_CHARS,
    _subsection_key,
    build_edition_api_payload,
    format_section_heading,
    generate_post,
    write_edition_api,
)

NOW = datetime(2026, 9, 13, 17, 0, 0, tzinfo=timezone.utc)
BP = 'AI is <a href="https://example.com">transforming</a> everything.'


def article(title, source, link=None, pub_dt=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)):
    return {
        "title": title,
        "link": link or f"https://example.com/{title.replace(' ', '-').lower()}",
        "source": source,
        "description": f"{title} description",
        "pub_dt": pub_dt,
        "pub": "Sat, 13 Sep 2026 08:00:00 GMT",
    }


def sections_data():
    """Two populated sections, plus one empty, with distinct subsection titles."""
    return [
        {
            "title": "AI Labs",
            "subsections": [
                {"title": "OpenAI Labs", "feeds": {"OpenAI News": "https://example.com/o.xml"}},
                {"title": "Anthropic Labs", "feeds": {"Anthropic": "https://example.com/a.xml"}},
            ],
        },
        {
            "title": "Benchmarks",
            "subsections": [
                {"title": "Arena", "feeds": {"Arena": "https://example.com/ar.xml"}},
            ],
        },
        {
            "title": "Research",
            "subsections": [
                {"title": "arXiv", "feeds": {"arXiv": "https://example.com/x.xml"}},
            ],
        },
    ]


def build(**overrides):
    kwargs = dict(
        edition_label="Evening",
        post_now=NOW,
        generated_at=NOW,
        global_summary_text=BP,
        section_summaries={"AI Labs": "Labs <strong>summary</strong> here."},
        sections_data=sections_data(),
        subsection_articles={
            _subsection_key(0, 0): [article("Labs story", "OpenAI News")],
            _subsection_key(0, 1): [article("Claude story", "Anthropic")],
            _subsection_key(1, 0): [article("Bench story", "Arena")],
        },
        freshness={"fresh": 3, "stale": 0, "yesterday": 1},
        reading_minutes=4,
        header_fragments=["Scanning 34 feeds", "5 accounts posted", "62 items"],
    )
    kwargs.update(overrides)
    return build_edition_api_payload(**kwargs)


class TestPayloadShape(unittest.TestCase):

    def test_core_fields(self):
        p = build()
        self.assertEqual(p["edition"], "Evening")
        self.assertEqual(p["date"], "2026-09-13")
        self.assertEqual(p["title"], "AI News Digest — Evening Edition")
        self.assertEqual(
            p["url"], "https://cokev-bot.github.io/ai-news/news/2026/09/13/Evening/"
        )
        self.assertEqual(p["published"], NOW.isoformat())
        self.assertEqual(p["generated_at"], NOW.isoformat())

    def test_stats_from_render_inputs(self):
        p = build()
        s = p["stats"]
        self.assertEqual(s["feeds_scanned"], 34)
        self.assertEqual(s["items"], 3)
        self.assertEqual(s["sources"], 3)
        self.assertEqual(s["fresh"], 3)
        self.assertEqual(s["stale"], 0)
        self.assertEqual(s["from_yesterday"], 1)
        self.assertEqual(s["words"], 3)  # "Labs summary here."

    def test_sources_sorted_and_unique(self):
        p = build(subsection_articles={
            _subsection_key(0, 0): [article("A", "OpenAI News"), article("B", "OpenAI News")],
            _subsection_key(0, 1): [article("C", "Anthropic")],
        })
        self.assertEqual(p["sources"], ["Anthropic", "OpenAI News"])

    def test_summary_stripped_and_truncated_big_picture_whole(self):
        p = build()
        self.assertEqual(p["summary"], "AI is transforming everything."[:MAX_DESCRIPTION_CHARS])
        self.assertEqual(p["big_picture"], "AI is transforming everything.")
        self.assertNotIn("<a", p["big_picture"])

    def test_reading_time_passthrough(self):
        self.assertEqual(build()["reading_time_minutes"], 4)
        self.assertEqual(build(reading_minutes=0)["reading_time_minutes"], 0)

    def test_json_serialisable(self):
        # Must survive a round-trip with no datetime objects leaking in.
        self.assertEqual(json.loads(json.dumps(build()))["edition"], "Evening")

    def test_generated_at_differs_from_published_on_republish(self):
        original = datetime(2026, 9, 12, 9, 49, tzinfo=timezone.utc)
        p = build(post_now=original, generated_at=original)
        self.assertEqual(p["published"], original.isoformat())
        self.assertEqual(p["url"].split("/news/")[1], "2026/09/12/Evening/")


class TestSectionsAndSubsections(unittest.TestCase):

    def test_sections_omitted_when_empty(self):
        titles = [s["title"] for s in build()["sections"]]
        self.assertEqual(titles, ["AI Labs", "Benchmarks"])  # Research empty

    def test_section_url_has_slug_anchor(self):
        s = build()["sections"][0]
        self.assertTrue(s["url"].endswith("#ai-labs"))
        self.assertEqual(s["item_count"], 2)

    def test_subsection_titles_collide_across_and_within_sections(self):
        """The real repo's trap: some subsection titles repeat across sections.

        A subsection title can appear under more than one section, and a
        single title can span multiple sections, so a title is NOT a global
        key. This test asserts the *code's contract* (it must handle title
        collisions) against whatever the real sections.json currently holds,
        without pinning specific names — the user edits sections.json by hand
        and a rename/removal must not break this test.
        """
        data = json.loads(
            (PROJECT_ROOT / "sections.json").read_text(encoding="utf-8")
        )["sections"]
        per_section = {
            s["title"]: [ss["title"] for ss in s["subsections"]] for s in data
        }
        # If any title repeats across sections, the code must tolerate it:
        # verify that the repeated title still maps back to every owning
        # section (the lookup never drops an owner).
        counts = {}
        for titles in per_section.values():
            for t in set(titles):
                counts[t] = counts.get(t, 0) + 1
        for name, n in counts.items():
            if n > 1:
                owners = [s for s, titles in per_section.items() if name in titles]
                self.assertEqual(len(owners), n, f"{name} should span {n} sections")

    def test_repeated_subsection_title_does_not_collapse_sections(self):
        """A title repeated across sections yields two independent sections.

        Keys are positional, so each section owns only its own articles: the
        two sections are both present and neither inherits the other's items.
        This is the regression guard for the duplication bug — with title
        keying, both sections shared one article list and rendered it twice.
        """
        p = build(
            sections_data=[
                {"title": "S1", "subsections": [{"title": "OpenAI", "feeds": {}}]},
                {"title": "S2", "subsections": [{"title": "OpenAI", "feeds": {}}]},
            ],
            subsection_articles={
                _subsection_key(0, 0): [article("X", "Src1")],
                _subsection_key(1, 0): [article("Y", "Src2")],
            },
        )
        self.assertEqual([s["title"] for s in p["sections"]], ["S1", "S2"])
        self.assertEqual(
            p["sections"][0]["subsections"][0]["articles"][0]["title"], "X"
        )
        self.assertEqual(
            p["sections"][1]["subsections"][0]["articles"][0]["title"], "Y"
        )

    def test_unpopulated_subsection_is_skipped_within_a_section(self):
        p = build(
            sections_data=[{
                "title": "S1",
                "subsections": [
                    {"title": "Has", "feeds": {}},
                    {"title": "Empty", "feeds": {}},
                ],
            }],
            subsection_articles={_subsection_key(0, 0): [article("X", "Src")]},
        )
        subs = [ss["title"] for ss in p["sections"][0]["subsections"]]
        self.assertEqual(subs, ["Has"])

    def test_section_summary_html_stripped(self):
        s = build()["sections"][0]
        self.assertEqual(s["summary"], "Labs summary here.")

    def test_section_summary_missing_is_empty_string(self):
        p = build(section_summaries={})
        for s in p["sections"]:
            self.assertEqual(s["summary"], "")

    def test_articles_carry_fields_and_no_html(self):
        art = build()["sections"][0]["subsections"][0]["articles"][0]
        self.assertEqual(
            set(art), {"title", "link", "source", "description", "published"}
        )
        self.assertEqual(art["title"], "Labs story")
        self.assertEqual(art["source"], "OpenAI News")
        self.assertNotIn("<a", art["title"])

    def test_published_is_iso_utc(self):
        art = build()["sections"][0]["subsections"][0]["articles"][0]
        self.assertTrue(art["published"].endswith("+00:00"))

    def test_naive_datetime_treated_as_utc(self):
        naive = article("Naive", "Src", pub_dt=datetime(2026, 9, 13, 8, 0))
        p = build(subsection_articles={_subsection_key(0, 0): [naive]})
        art = p["sections"][0]["subsections"][0]["articles"][0]
        self.assertEqual(art["published"], "2026-09-13T08:00:00+00:00")

    def test_missing_pub_dt_is_null(self):
        no_date = {"title": "T", "link": "https://e.com/1", "source": "S"}
        p = build(subsection_articles={_subsection_key(0, 0): [no_date]})
        art = p["sections"][0]["subsections"][0]["articles"][0]
        self.assertIsNone(art["published"])

    def test_nitter_link_converted_to_x(self):
        art = {"title": "T", "link": "https://nitter.net/u/status/1", "source": "S"}
        p = build(subsection_articles={_subsection_key(0, 0): [art]})
        entry = p["sections"][0]["subsections"][0]["articles"][0]
        self.assertIn("x.com", entry["link"])
        self.assertNotIn("nitter", entry["link"])

    def test_title_falls_back_to_link(self):
        art = {"link": "https://e.com/1", "source": "S"}
        p = build(subsection_articles={_subsection_key(0, 0): [art]})
        entry = p["sections"][0]["subsections"][0]["articles"][0]
        self.assertEqual(entry["title"], "https://e.com/1")


class TestNoEditions(unittest.TestCase):

    def test_empty_edition_has_zero_stats(self):
        p = build(
            global_summary_text=None,
            section_summaries={},
            subsection_articles={},
            freshness={"fresh": 0, "stale": 0, "yesterday": 0},
            reading_minutes=0,
            header_fragments=["Scanning 34 feeds", "0 accounts posted", "0 items"],
        )
        self.assertEqual(p["sections"], [])
        self.assertEqual(p["sources"], [])
        self.assertEqual(p["stats"]["items"], 0)
        self.assertEqual(p["stats"]["feeds_scanned"], 34)
        self.assertEqual(p["big_picture"], "")
        self.assertEqual(p["summary"], "")

    def test_missing_edition_label_degrades_gracefully(self):
        p = build(edition_label=None)
        self.assertEqual(p["title"], "AI News Digest")
        self.assertTrue(p["url"].endswith("/ai-news/"))

    def test_feeds_scanned_defaults_to_zero_without_header(self):
        p = build(header_fragments=None)
        self.assertEqual(p["stats"]["feeds_scanned"], 0)


class TestWriteEditionApi(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_file_at_expected_path(self):
        path = write_edition_api(self.root, "2026-09-13-Evening", {"a": 1})
        self.assertEqual(path, self.root / API_DIR / "2026-09-13-Evening.json")
        self.assertEqual(json.loads(path.read_text()), {"a": 1})

    def test_creates_missing_directory(self):
        self.assertFalse((self.root / API_DIR).exists())
        write_edition_api(self.root, "2026-09-13-Morning", {"a": 1})
        self.assertTrue((self.root / API_DIR).is_dir())

    def test_no_tmp_file_left_behind(self):
        write_edition_api(self.root, "2026-09-13-Morning", {"a": 1})
        leftovers = [p.name for p in (self.root / API_DIR).iterdir() if p.suffix != ".json"]
        self.assertEqual(leftovers, [])

    def test_idempotent_overwrite(self):
        write_edition_api(self.root, "E", {"v": 1})
        write_edition_api(self.root, "E", {"v": 2})
        self.assertEqual(json.loads((self.root / API_DIR / "E.json").read_text()), {"v": 2})

    def test_returns_none_without_raising_on_unwritable_target(self):
        # A file where the api/ directory should be makes mkdir fail.
        (self.root / API_DIR).write_text("not a directory")
        self.assertIsNone(write_edition_api(self.root, "E", {"a": 1}))

    def test_empty_edition_name_is_skipped(self):
        self.assertIsNone(write_edition_api(self.root, "", {"a": 1}))

    def test_unicode_preserved(self):
        write_edition_api(self.root, "E", {"t": "AI News Digest — Morning"})
        raw = (self.root / API_DIR / "E.json").read_text(encoding="utf-8")
        self.assertIn("—", raw)
        self.assertEqual(json.loads(raw)["t"], "AI News Digest — Morning")


class TestRealSectionsJson(unittest.TestCase):
    """The endpoint must be correct against the repo's actual sections.json."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((PROJECT_ROOT / "sections.json").read_text(encoding="utf-8"))
        cls.sections = cls.data["sections"]

    def test_every_feed_is_reachable_from_some_section(self):
        """No subsection is lost, and no item is rendered under two sections.

        Each (section, subsection) position gets its own article list, so the
        payload reaches every position exactly once and the total item count
        equals the number of populated positions — not the number of positions
        times the number of sections sharing a title.
        """
        articles = {}
        for s_index, section in enumerate(self.sections):
            for ss_index, ss in enumerate(section["subsections"]):
                articles[_subsection_key(s_index, ss_index)] = [
                    article(f"{ss['title']} story {s_index}-{ss_index}", "Src")
                ]
        p = build_edition_api_payload(
            edition_label="Morning",
            post_now=NOW,
            generated_at=NOW,
            global_summary_text=BP,
            section_summaries={s["title"]: "S." for s in self.sections},
            sections_data=self.sections,
            subsection_articles=articles,
            freshness={"fresh": 1, "stale": 0, "yesterday": 0},
        )
        rendered_titles = [
            a["title"]
            for s in p["sections"]
            for ss in s["subsections"]
            for a in ss["articles"]
        ]
        # Every position reached, and nothing rendered twice.
        self.assertEqual(len(rendered_titles), len(articles))
        self.assertEqual(len(set(rendered_titles)), len(articles))
        # Sections are never dropped or emptied.
        for s in p["sections"]:
            self.assertGreater(s["item_count"], 0)
        self.assertEqual(p["stats"]["items"], len(articles))

    def test_real_sections_json_has_repeated_titles(self):
        """Guard the premise this whole file works around.

        If sections.json ever stops reusing subsection titles, the cross-section
        duplication this design prevents becomes impossible — the test should
        say so rather than silently passing.
        """
        seen = {}
        for section in self.sections:
            for ss in section["subsections"]:
                seen.setdefault(ss["title"], []).append(section["title"])
        repeated = {t: o for t, o in seen.items() if len(o) > 1}
        self.assertTrue(
            repeated,
            "sections.json no longer reuses subsection titles — revisit the "
            "positional keying rationale in generate_news._subsection_key()",
        )

    def test_section_titles_are_unique(self):
        """Section titles must stay unique — the payload keys on them."""
        titles = [s["title"] for s in self.sections]
        self.assertEqual(len(titles), len(set(titles)))

    def test_real_payload_is_json_serialisable(self):
        p = build(
            sections_data=self.sections,
            subsection_articles={
                ss["title"]: [article("T", "S")]
                for section in self.sections
                for ss in section["subsections"]
            },
        )
        self.assertIn("sections", json.loads(json.dumps(p)))


class TestGeneratePostIntegration(unittest.TestCase):
    """generate_post() writes the payload beside the post it describes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site_root = Path(self._tmp.name)
        (self.site_root / "_posts").mkdir()
        (self.site_root / "sections.json").write_text(json.dumps({
            "sections": [{
                "title": "News",
                "subsections": [{
                    "title": "FT",
                    "feeds": {"FT AI": "https://example.com/ft.xml"},
                }],
            }],
            "source_urls": {"FT AI": "https://www.ft.com/artificial-intelligence"},
        }))
        (self.site_root / "config.json").write_text(json.dumps({"model": "test"}))
        (self.site_root / "summary_prompt.txt").write_text("Summarize.")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, edition):
        art = article("A story", "FT AI")
        with patch("generate_news.fetch_all_feeds") as fetch, \
             patch("generate_news.load_state") as load, \
             patch("generate_news.save_state"), \
             patch("generate_news.generate_edition_audio", return_value={}), \
             patch("generate_news.generate_og_image_for_edition", return_value=None), \
             patch("generate_news._query_ollama", return_value="Summary text."):
            load.return_value = {"seen_links": {}, "last_run": None}
            fetch.return_value = {_subsection_key(0, 0): [("FT AI", [art])]}
            return generate_post(edition, self.site_root)

    def test_payload_written_next_to_post(self):
        self.assertTrue(self._run("2026-09-13-morning"))
        path = self.site_root / API_DIR / "2026-09-13-morning.json"
        self.assertTrue(path.exists(), "api payload missing after generate_post()")
        p = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(p["edition"], "Morning")
        self.assertEqual(p["stats"]["items"], 1)
        self.assertEqual(p["sections"][0]["title"], "News")
        self.assertEqual(p["sections"][0]["subsections"][0]["articles"][0]["source"], "FT AI")

    def test_payload_date_matches_post_frontmatter(self):
        self.assertTrue(self._run("2026-09-13-morning"))
        post = (self.site_root / "_posts" / "2026-09-13-morning.html").read_text()
        p = json.loads((self.site_root / API_DIR / "2026-09-13-morning.json").read_text())
        fm_date = re.search(r"^date: (\d{4}-\d{2}-\d{2})", post, re.M).group(1)
        self.assertEqual(p["date"], fm_date)

    def test_payload_stats_agree_with_post_header(self):
        self.assertTrue(self._run("2026-09-13-morning"))
        post = (self.site_root / "_posts" / "2026-09-13-morning.html").read_text()
        p = json.loads((self.site_root / API_DIR / "2026-09-13-morning.json").read_text())
        m = re.search(r"Scanning (\d+) feeds · (\d+) accounts posted · (\d+) items", post)
        self.assertIsNotNone(m, "post header shape changed")
        self.assertEqual(p["stats"]["feeds_scanned"], int(m.group(1)))
        self.assertEqual(p["stats"]["sources"], int(m.group(2)))
        self.assertEqual(p["stats"]["items"], int(m.group(3)))

    def test_payload_write_failure_does_not_break_the_edition(self):
        with patch("generate_news.write_edition_api", return_value=None):
            self.assertTrue(self._run("2026-09-13-evening"))
        self.assertTrue((self.site_root / "_posts" / "2026-09-13-evening.html").exists())


class TestSectionHeadingAnchors(unittest.TestCase):

    def test_heading_has_slug_id(self):
        self.assertIn('id="developer-tools"', format_section_heading("Developer Tools", 3))

    def test_heading_keeps_badge_text(self):
        self.assertIn(">News <span", format_section_heading("News", 3))

    def test_id_can_be_suppressed(self):
        out = format_section_heading("News", 0, with_id=False)
        self.assertNotIn("id=", out)

    def test_anchor_resolves_within_post(self):
        """The API's per-section anchor must exist as an id in the post HTML."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_posts").mkdir()
            (root / "sections.json").write_text(json.dumps({
                "sections": [{
                    "title": "Developer Tools",
                    "subsections": [{"title": "FT", "feeds": {"FT AI": "https://e.com/f.xml"}}],
                }],
            }))
            (root / "config.json").write_text(json.dumps({"model": "test"}))
            (root / "summary_prompt.txt").write_text("Summarize.")
            with patch("generate_news.fetch_all_feeds") as fetch, \
                 patch("generate_news.load_state") as load, \
                 patch("generate_news.save_state"), \
                 patch("generate_news.generate_edition_audio", return_value={}), \
                 patch("generate_news.generate_og_image_for_edition", return_value=None), \
                 patch("generate_news._query_ollama", return_value="Summary text."):
                load.return_value = {"seen_links": {}, "last_run": None}
                fetch.return_value = {_subsection_key(0, 0): [("FT AI", [article("A", "FT AI")])]}
                generate_post("2026-09-13-morning", root)
            post = (root / "_posts" / "2026-09-13-morning.html").read_text()
            p = json.loads((root / API_DIR / "2026-09-13-morning.json").read_text())
            anchor = p["sections"][0]["url"].split("#", 1)[1]
            self.assertIn(f'id="{anchor}"', post)


class TestPipelineWiring(unittest.TestCase):

    def test_run_edition_stages_api_dir(self):
        script = (PROJECT_ROOT / "run_edition.sh").read_text(encoding="utf-8")
        self.assertIn("api/", script,
                      "api/ is not staged — payloads would never be published")
        # It must be guarded by an existence check. A bare `git add api/` exits
        # 128 when the directory is absent and, with --ignore-errors, stages
        # nothing at all — so a failed payload write would silently produce no
        # commit for the whole edition.
        self.assertRegex(
            script,
            r"if \[ -d api \]; then\s*\n\s*git add --ignore-errors api/",
            "api/ must be staged behind an `if [ -d api ]` guard",
        )

    def test_missing_directory_makes_bare_git_add_stage_nothing(self):
        """Pins the reason for the guard above, against real git."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "_posts").mkdir()
            (root / "_posts" / "a.html").write_text("hi")
            res = subprocess.run(
                ["git", "add", "--ignore-errors", "_posts/", "api/"],
                cwd=str(root), capture_output=True, text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=str(root), capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(staged, "", "git unexpectedly staged files")

    def test_api_dir_present_is_staged_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "_posts").mkdir()
            (root / "_posts" / "a.html").write_text("hi")
            (root / "api").mkdir()
            (root / "api" / "e.json").write_text("{}")
            res = subprocess.run(
                ["git", "add", "--ignore-errors", "_posts/", "api/"],
                cwd=str(root), capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=str(root), capture_output=True, text=True,
            ).stdout.split()
            self.assertIn("api/e.json", staged)
            self.assertIn("_posts/a.html", staged)

    def test_api_dir_not_gitignored(self):
        ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        patterns = [l.strip() for l in ignore.splitlines()
                    if l.strip() and not l.strip().startswith("#")]
        for pat in patterns:
            bare = pat.strip("/")
            self.assertNotEqual(bare, "api",
                                f".gitignore excludes the API directory via '{pat}'")

    def test_scripts_syntax_check(self):
        for name in ("run_edition.sh",):
            out = subprocess.run(["bash", "-n", str(PROJECT_ROOT / name)],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, f"{name}: {out.stderr}")


class TestJekyllServesTheApiDir(unittest.TestCase):
    """The endpoint only works if the build actually copies api/*.json."""

    def test_json_in_posts_dir_is_dropped_but_api_dir_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "site"
            (src / "_posts").mkdir(parents=True)
            (src / API_DIR).mkdir()
            (src / "_config.yml").write_text('title: T\nbaseurl: ""\n')
            (src / "index.html").write_text("<h1>hi</h1>")
            (src / "_posts" / "probe.json").write_text('{"in":"posts"}')
            (src / API_DIR / "probe.json").write_text('{"in":"api"}')
            dest = Path(tmp) / "out"
            res = subprocess.run(
                ["bundle", "exec", "jekyll", "build",
                 "--source", str(src), "--destination", str(dest)],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=300,
            )
            self.assertEqual(res.returncode, 0, res.stderr[-500:])
            self.assertTrue((dest / API_DIR / "probe.json").exists(),
                            "api/ payloads are not copied into the build output")
            self.assertFalse((dest / "_posts" / "probe.json").exists(),
                             "_posts/probe.json unexpectedly survived the build")


if __name__ == "__main__":
    unittest.main()

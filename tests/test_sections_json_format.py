"""Format and shape checks for sections.json.

The point of this file is to assert that sections.json is *well-formed and
self-consistent* — NOT to pin how many feeds/sections it contains or which
specific names appear where. The user edits sections.json by hand to add and
remove sources, and must not have to update code or tests every time.

Accepted assertions (what belongs here):
  - the file exists and is valid JSON
  - it has the expected top-level shape (object with "sections" + "source_urls")
  - every section has a title and a non-empty "subsections" list
  - every subsection has a title and a "feeds" dict (possibly empty)
  - every feed URL is a non-empty string
  - every feed name in sections has a matching source_urls entry
  - source_urls values are http(s) URLs

Rejected assertions (what must NOT be here):
  - "there are N feeds" / "there are N sections" / "section X has N subsections"
  - "feed Y is under section Z" — a removal/rename should not break tests
  - any hardcoded feed name or URL

For live-network checks of a *newly added* feed, write a separate
test_new_feed_<name>.py that carries the URL it checks; do not centralize the
list of "known feeds" in a shared structure that an edit must update.
"""

import json
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTIONS_FILE = PROJECT_ROOT / "sections.json"


def _load_raw() -> Any:
    with SECTIONS_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _sections(data: object) -> list[dict]:
    """Return the sections array for either supported top-level shape."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("sections", [])
    return []


class TestSectionsJsonFormat(unittest.TestCase):
    """sections.json must exist and parse as valid JSON."""

    def test_file_exists(self):
        self.assertTrue(SECTIONS_FILE.exists(), "sections.json must exist")

    def test_file_is_valid_json(self):
        # json.load raises on malformed input; a non-empty result is expected.
        data = _load_raw()
        self.assertIsNotNone(data, "sections.json must not be empty/None")


class TestSectionsJsonShape(unittest.TestCase):
    """The top level must be an object with 'sections' and 'source_urls'."""

    def test_is_object_with_required_keys(self):
        data = _load_raw()
        self.assertIsInstance(data, dict, "sections.json should be an object, not a list")
        self.assertIn("sections", data)
        self.assertIn("source_urls", data)

    def test_sections_is_a_nonempty_list(self):
        data = _load_raw()
        sections = data["sections"]
        self.assertIsInstance(sections, list)
        self.assertGreater(len(sections), 0, "at least one section is expected")


class TestSectionsJsonStructure(unittest.TestCase):
    """Every section/subsection/feed carries the required fields."""

    def test_every_section_has_title_and_subsections(self):
        for section in _sections(_load_raw()):
            self.assertTrue(section.get("title"), "every section needs a title")
            self.assertIsInstance(section.get("subsections"), list,
                                  f"section '{section.get('title')}' needs a subsections list")

    def test_every_subsection_has_title_and_feeds(self):
        for section in _sections(_load_raw()):
            for sub in section.get("subsections", []):
                self.assertTrue(sub.get("title"), "every subsection needs a title")
                self.assertIsInstance(sub.get("feeds"), dict,
                                      f"subsection '{sub.get('title')}' needs a feeds dict")

    def test_every_feed_url_is_a_nonempty_string(self):
        for section in _sections(_load_raw()):
            for sub in section.get("subsections", []):
                for name, url in sub.get("feeds", {}).items():
                    self.assertTrue(name, "every feed needs a non-empty name")
                    self.assertIsInstance(url, str, f"feed '{name}' URL must be a string")
                    self.assertTrue(url.strip(), f"feed '{name}' has an empty URL")


class TestSectionsJsonConsistency(unittest.TestCase):
    """sections and source_urls must agree, whatever the current contents."""

    @classmethod
    def setUpClass(cls):
        data = _load_raw()
        cls.sections = _sections(data)
        cls.source_urls = data.get("source_urls", {}) if isinstance(data, dict) else {}
        cls.all_feed_names = []
        for section in cls.sections:
            for sub in section.get("subsections", []):
                for name in sub.get("feeds", {}):
                    cls.all_feed_names.append(name)

    def test_every_feed_has_source_url(self):
        missing = [n for n in self.all_feed_names if n not in self.source_urls]
        self.assertEqual(
            missing, [],
            f"feed names missing from source_urls: {missing}",
        )

    def test_source_urls_are_http_urls(self):
        for name, url in self.source_urls.items():
            self.assertTrue(
                url.startswith("http://") or url.startswith("https://"),
                f"source_urls['{name}'] = '{url}' is not an http(s) URL",
            )

    def test_source_urls_do_not_grossly_diverge(self):
        # Extra source_urls entries are harmless but a wholesale mismatch is a
        # sign the file was edited inconsistently.
        extra = set(self.source_urls) - set(self.all_feed_names)
        self.assertLessEqual(
            len(extra), len(self.all_feed_names),
            f"too many source_urls entries missing from sections: {extra}",
        )


if __name__ == "__main__":
    unittest.main()

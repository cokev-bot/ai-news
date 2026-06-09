"""Tests for tuning config (get_tuning) and log rotation (RotatingFileHandler)."""

import json
import logging
import logging.handlers
import unittest
from pathlib import Path
from unittest.mock import patch

from generate_news import (
    DEFAULT_CONFIG,
    get_tuning,
    is_duplicate,
    fetch_feed,
)


class TestGetTuningDefaultFallback(unittest.TestCase):
    """get_tuning should merge user config over DEFAULT_CONFIG['tuning'] defaults."""

    def test_missing_config_file_returns_defaults(self):
        """When config.json is absent, get_tuning returns the DEFAULT_CONFIG tuning values."""
        site_root = Path("/nonexistent/path")
        result = get_tuning(site_root)
        expected = DEFAULT_CONFIG["tuning"]
        self.assertEqual(result["max_items_per_source"], expected["max_items_per_source"])
        self.assertEqual(result["max_age_days"], expected["max_age_days"])
        self.assertAlmostEqual(result["title_sim_threshold"], expected["title_sim_threshold"])
        self.assertEqual(result["cross_edition_dedup_hours"], expected["cross_edition_dedup_hours"])

    def test_partial_override_keeps_defaults(self):
        """If config.json specifies only some tuning keys, the rest come from defaults."""
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a config with only one tuning key overridden
            partial_config = {
                "model": "test-model",
                "tuning": {
                    "max_age_days": 14,
                },
            }
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(partial_config), encoding="utf-8")

            result = get_tuning(Path(tmpdir))
            # Overridden key
            self.assertEqual(result["max_age_days"], 14)
            # Defaults for the rest
            self.assertEqual(result["max_items_per_source"], DEFAULT_CONFIG["tuning"]["max_items_per_source"])
            self.assertAlmostEqual(result["title_sim_threshold"], DEFAULT_CONFIG["tuning"]["title_sim_threshold"])
            self.assertEqual(result["cross_edition_dedup_hours"], DEFAULT_CONFIG["tuning"]["cross_edition_dedup_hours"])

    def test_full_override(self):
        """All tuning keys can be overridden from config.json."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_config = {
                "model": "test-model",
                "tuning": {
                    "max_items_per_source": 5,
                    "max_age_days": 3,
                    "title_sim_threshold": 0.8,
                    "cross_edition_dedup_hours": 48,
                },
            }
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(custom_config), encoding="utf-8")

            result = get_tuning(Path(tmpdir))
            self.assertEqual(result["max_items_per_source"], 5)
            self.assertEqual(result["max_age_days"], 3)
            self.assertAlmostEqual(result["title_sim_threshold"], 0.8)
            self.assertEqual(result["cross_edition_dedup_hours"], 48)

    def test_no_tuning_key_in_config_returns_defaults(self):
        """If config.json exists but has no 'tuning' key, defaults are returned."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"model": "some-model"}
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = get_tuning(Path(tmpdir))
            self.assertEqual(result["max_items_per_source"], DEFAULT_CONFIG["tuning"]["max_items_per_source"])
            self.assertEqual(result["max_age_days"], DEFAULT_CONFIG["tuning"]["max_age_days"])


class TestIsDuplicateTuningParams(unittest.TestCase):
    """is_duplicate should accept and use tuning keyword arguments."""

    def test_high_threshold_skips_similar(self):
        """With a very high threshold, similar-but-not-identical titles are not caught."""
        art = {"title": "OpenAI announces new research lab expansion", "link": "https://example.com/1", "description": "Big news"}
        seen_links = {}
        seen = []
        # Default threshold (0.4) catches partial overlap "openai announces new" vs "openAI announces new research"
        self.assertTrue(is_duplicate(
            art, seen + [{"title": "OpenAI announces new research partnership", "link": "https://example.com/2", "description": ""}],
            seen_links,
            title_sim_threshold=0.4,
        ))
        # Very high threshold should NOT catch similar-but-not-identical titles
        self.assertFalse(is_duplicate(
            art, seen + [{"title": "OpenAI announces new research partnership", "link": "https://example.com/2", "description": ""}],
            seen_links,
            title_sim_threshold=0.99,
        ))

    def test_cross_edition_dedup_hours_param(self):
        """Cross-edition dedup window can be overridden."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        art = {"title": "Breaking: New AI model released", "link": "https://example.com/new", "description": ""}
        # seen_links entry from 12 hours ago — within the 24h default window
        seen_links = {
            "https://example.com/old": {
                "edition": "2026-06-09-morning",
                "feed": "Test",
                "title": "Breaking: New AI model released",
                "description": "",
                "seen_at": (now - timedelta(hours=12)).isoformat(),
            }
        }
        # Default 24h window catches it
        self.assertTrue(is_duplicate(art, [], seen_links, now=now, cross_edition_dedup_hours=24))
        # 1h window does NOT catch a 12h-old entry
        self.assertFalse(is_duplicate(art, [], seen_links, now=now, cross_edition_dedup_hours=1))

    def test_default_params_match_module_constants(self):
        """When not overridden, is_duplicate uses the module-level constants."""
        # Exact same title + link already in seen_links
        art = {"title": "Test", "link": "https://example.com", "description": ""}
        self.assertTrue(is_duplicate(art, [], {"https://example.com": {"edition": "x", "feed": "f", "title": "Test", "description": ""}}))


class TestLogRotation(unittest.TestCase):
    """Verify that the root logger uses a RotatingFileHandler for generate_news.log."""

    def test_root_logger_has_rotating_file_handler(self):
        """The root logger should have at least one RotatingFileHandler."""
        import generate_news
        root_handlers = logging.getLogger().handlers
        rotating_handlers = [
            h for h in root_handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        self.assertTrue(
            len(rotating_handlers) > 0,
            "Expected at least one RotatingFileHandler on the root logger, "
            f"but found: {[type(h).__name__ for h in root_handlers]}"
        )

    def test_rotating_file_handler_settings(self):
        """RotatingFileHandler should be configured with maxBytes=10MB and backupCount=3."""
        root_handlers = logging.getLogger().handlers
        rotating_handlers = [
            h for h in root_handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        self.assertTrue(len(rotating_handlers) > 0)
        handler = rotating_handlers[0]
        self.assertEqual(handler.maxBytes, 10 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)


if __name__ == "__main__":
    unittest.main()
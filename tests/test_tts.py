#!/usr/bin/env python3
"""Tests for text-to-speech audio generation."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from generate_news import (
    DEFAULT_TTS_CONFIG,
    get_tts_config,
    _strip_html,
    generate_audio,
    _slugify,
    generate_edition_audio,
    audio_player_html,
)


class TestStripHtml(unittest.TestCase):
    """Tests for _strip_html()."""

    def test_removes_html_tags(self):
        self.assertEqual(_strip_html("<p>Hello world</p>"), "Hello world")

    def test_extracts_link_text(self):
        self.assertEqual(
            _strip_html('See <a href="https://example.com">this link</a> for more'),
            "See this link for more",
        )

    def test_normalizes_whitespace(self):
        self.assertEqual(_strip_html("Hello   <br>   world"), "Hello world")

    def test_empty_string(self):
        self.assertEqual(_strip_html(""), "")

    def test_complex_html(self):
        text = '<strong>OpenAI</strong>: <a href="https://x.com/1">new model</a> released'
        self.assertEqual(_strip_html(text), "OpenAI : new model released")

    def test_nested_tags(self):
        text = '<p><strong>Bold</strong> and <em>italic</em></p>'
        self.assertEqual(_strip_html(text), "Bold and italic")


class TestSlugify(unittest.TestCase):
    """Tests for _slugify()."""

    def test_lowercase(self):
        self.assertEqual(_slugify("News"), "news")

    def test_spaces_to_hyphens(self):
        self.assertEqual(_slugify("AI Labs"), "ai-labs")

    def test_special_chars_removed(self):
        self.assertEqual(_slugify("R&D / Engineering!"), "rd-engineering")

    def test_length_limit(self):
        long_title = "A" * 100
        self.assertLessEqual(len(_slugify(long_title)), 60)

    def test_multiple_hyphens_collapsed(self):
        self.assertEqual(_slugify("Hello --- World"), "hello-world")


class TestGetTtsConfig(unittest.TestCase):
    """Tests for get_tts_config()."""

    def test_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps({"model": "test"}))
            with patch("generate_news.load_config", return_value={"model": "test"}):
                cfg = get_tts_config(Path(td))
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["voice"], "en-US-AriaNeural")
            self.assertEqual(cfg["rate"], "+0%")

    def test_user_overrides(self):
        user_cfg = {"model": "test", "tts": {"voice": "en-GB-RyanNeural", "rate": "+10%"}}
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps(user_cfg))
            with patch("generate_news.load_config", return_value=user_cfg):
                cfg = get_tts_config(Path(td))
            self.assertEqual(cfg["voice"], "en-GB-RyanNeural")
            self.assertEqual(cfg["rate"], "+10%")
            self.assertTrue(cfg["enabled"])  # default preserved

    def test_disabled(self):
        user_cfg = {"model": "test", "tts": {"enabled": False}}
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps(user_cfg))
            with patch("generate_news.load_config", return_value=user_cfg):
                cfg = get_tts_config(Path(td))
            self.assertFalse(cfg["enabled"])


class TestGenerateAudio(unittest.TestCase):
    """Tests for generate_audio()."""

    def test_empty_text_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            result = generate_audio("", Path(td) / "out.mp3")
            self.assertFalse(result)

    def test_whitespace_only_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            result = generate_audio("   ", Path(td) / "out.mp3")
            self.assertFalse(result)

    @patch("generate_news.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "out.mp3"
            # Create a fake MP3 file so the size check passes
            out_path.write_bytes(b"\xff" * 500)
            mock_run.return_value = MagicMock(returncode=0)
            result = generate_audio("Hello world", out_path)
            # The function checks if file exists and size > 100 after subprocess
            self.assertTrue(result)

    @patch("generate_news.subprocess.run")
    def test_edge_tts_failure_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        with tempfile.TemporaryDirectory() as td:
            result = generate_audio("Hello world", Path(td) / "out.mp3")
            self.assertFalse(result)

    @patch("generate_news.subprocess.run", side_effect=FileNotFoundError("no edge-tts"))
    def test_edge_tts_not_found(self, mock_run):
        with tempfile.TemporaryDirectory() as td:
            result = generate_audio("Hello world", Path(td) / "out.mp3")
            self.assertFalse(result)

    @patch("generate_news.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="edge-tts", timeout=120))
    def test_timeout_returns_false(self, mock_run):
        with tempfile.TemporaryDirectory() as td:
            result = generate_audio("Hello world", Path(td) / "out.mp3")
            self.assertFalse(result)


class TestAudioPlayerHtml(unittest.TestCase):
    """Tests for audio_player_html()."""

    def test_basic_player(self):
        html = audio_player_html("assets/audio/2026-06-10/news.mp3", "News summary")
        self.assertIn("controls", html)
        self.assertIn('preload="none"', html)
        self.assertIn("/ai-news/assets/audio/2026-06-10/news.mp3", html)
        self.assertIn("News summary", html)
        self.assertIn("audio/mpeg", html)

    def test_path_has_leading_slash(self):
        html = audio_player_html("/assets/audio/x.mp3", "test")
        self.assertIn("/ai-news/assets/audio/x.mp3", html)


class TestGenerateEditionAudio(unittest.TestCase):
    """Tests for generate_edition_audio()."""

    @patch("generate_news.generate_audio")
    def test_disabled_returns_empty(self, mock_gen):
        with tempfile.TemporaryDirectory() as td:
            cfg = {"model": "test", "tts": {"enabled": False}}
            result = generate_edition_audio("2026-06-10", Path(td), None, {}, config=cfg)
            self.assertEqual(result, {})
            mock_gen.assert_not_called()

    @patch("generate_news.generate_audio", return_value=True)
    def test_generates_section_audio(self, mock_gen):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Create the audio directory so generate_audio's parent.mkdir works
            (td_path / "assets" / "audio" / "2026-06-10-afternoon").mkdir(parents=True)
            # Create fake mp3 files so the exists/size check in generate_audio passes
            for slug in ["news"]:
                mp3 = td_path / "assets" / "audio" / "2026-06-10-afternoon" / f"{slug}.mp3"
                mp3.write_bytes(b"\xff" * 500)

            # Re-patch to actually create files (simulating edge-tts)
            def fake_generate(text, path, voice="en-US-AriaNeural", rate="+0%"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\xff" * 500)
                return True
            
            with patch("generate_news.generate_audio", side_effect=fake_generate):
                cfg = {"model": "test", "tts": {"enabled": True, "voice": "en-US-AriaNeural"}}
                result = generate_edition_audio(
                    "2026-06-10-afternoon", td_path,
                    "Big picture text here",
                    {"News": "News summary text"},
                    config=cfg,
                )
            self.assertIn("big-picture", result)
            self.assertIn("news", result)

    @patch("generate_news.generate_audio", return_value=False)
    def test_all_audio_fails_returns_empty(self, mock_gen):
        with tempfile.TemporaryDirectory() as td:
            cfg = {"model": "test", "tts": {"enabled": True}}
            result = generate_edition_audio(
                "2026-06-10-afternoon", Path(td),
                "Big picture",
                {"News": "News summary"},
                config=cfg,
            )
            self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
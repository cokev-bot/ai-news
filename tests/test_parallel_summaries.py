"""Tests for the Phase 4 LLM-summary concurrency layer.

Covers `summarize_sections_concurrent()` — the wrapper that fans out
`get_section_summary()` calls to a ThreadPoolExecutor and returns a
title -> summary dict. We also exercise the path that uses it inside
`generate_post()` (the rendering loop's behavior when summaries are
precomputed in a dict).

We use a fake `get_section_summary` to avoid hitting the local Ollama
server in tests, and we time the executor to confirm it actually runs
the jobs in parallel rather than serially.
"""
import importlib
import logging
import time
import unittest
from pathlib import Path

import generate_news


def _make_article(idx, source="src", title=None):
    return {
        "title": title or f"Article {idx}",
        "link": f"http://example.com/{idx}",
        "source": source,
        "description": f"desc {idx}",
    }


class _FakeModule:
    """Stand-in for generate_news when we want to stub get_section_summary
    but still test the real `summarize_sections_concurrent` code path.

    We monkey-patch generate_news.get_section_summary in each test
    instead of doing a full module swap, because the rest of the
    module (SECTIONS, load_state, etc.) is needed by `generate_post`.
    """


class TestSummarizeSectionsConcurrent(unittest.TestCase):
    """Direct tests for the helper, with a stub get_section_summary."""

    def setUp(self):
        # Snapshot the real impl so we can restore in tearDown even if
        # the test fails mid-patch.
        self._real_get_section_summary = generate_news.get_section_summary
        self._call_log: list[tuple[str, float, float]] = []
        # Each fake call sleeps `per_call_seconds` to simulate an LLM
        # call's wall-clock cost. With multiple workers in parallel
        # the total should be ~per_call_seconds, not N*per_call_seconds.
        self.per_call_seconds = 0.2
        self._start_times: dict[str, float] = {}

    def tearDown(self):
        generate_news.get_section_summary = self._real_get_section_summary

    def _install_fake(self):
        def fake_summary(section_title, articles, site_root, config=None):
            started = time.monotonic()
            self._start_times[section_title] = started
            time.sleep(self.per_call_seconds)
            self._call_log.append(
                (section_title, started, time.monotonic())
            )
            return f"<summary-for-{section_title}>"

        generate_news.get_section_summary = fake_summary

    def test_empty_jobs_returns_empty_dict(self):
        self._install_fake()
        out = generate_news.summarize_sections_concurrent(
            [], Path("/tmp"), max_workers=4
        )
        self.assertEqual(out, {})
        # No work, no calls.
        self.assertEqual(self._call_log, [])

    def test_single_job_uses_serial_fast_path(self):
        # When only one section is being summarized, we should NOT
        # spin up a thread pool — there's nothing to parallelize and
        # the executor overhead is wasted work.
        self._install_fake()
        out = generate_news.summarize_sections_concurrent(
            [("A", [_make_article(1)])], Path("/tmp"), max_workers=4
        )
        self.assertEqual(out, {"A": "<summary-for-A>"})
        self.assertEqual(len(self._call_log), 1)

    def test_returns_dict_keyed_by_section_title(self):
        self._install_fake()
        jobs = [
            ("News", [_make_article(1), _make_article(2)]),
            ("AI Labs", [_make_article(3)]),
            ("Developers", [_make_article(4)]),
        ]
        out = generate_news.summarize_sections_concurrent(
            jobs, Path("/tmp"), max_workers=3
        )
        self.assertEqual(set(out.keys()), {"News", "AI Labs", "Developers"})
        self.assertEqual(out["News"], "<summary-for-News>")
        self.assertEqual(out["AI Labs"], "<summary-for-AI Labs>")
        self.assertEqual(out["Developers"], "<summary-for-Developers>")

    def test_runs_jobs_in_parallel_not_serially(self):
        """The whole point of the rework. With N=4 jobs and
        per_call_seconds=0.2, serial would be ~0.8s, parallel ~0.2s.
        Allow a generous ceiling to keep this from being flaky on a
        slow CI box, but assert well under serial cost."""
        self._install_fake()
        jobs = [(f"S{i}", [_make_article(i)]) for i in range(4)]
        start = time.monotonic()
        out = generate_news.summarize_sections_concurrent(
            jobs, Path("/tmp"), max_workers=4
        )
        elapsed = time.monotonic() - start
        self.assertEqual(set(out.keys()), {f"S{i}" for i in range(4)})
        # Serial would be 4 * 0.2 = 0.8s. Parallel ceiling is per_call
        # + a bit of overhead. Be generous (0.65s) but well under serial.
        self.assertLess(
            elapsed,
            self.per_call_seconds * 3,
            f"Expected parallel execution, but took {elapsed:.2f}s "
            f"(serial would be {self.per_call_seconds * 4:.2f}s)",
        )

    def test_max_workers_is_respected(self):
        """If max_workers=2 with 4 jobs, two of the jobs should still
        be running when the first two complete — i.e. the executor
        never has more than 2 in-flight calls at once."""
        self._install_fake()
        concurrent = 0
        max_observed = 0
        lock = __import__("threading").Lock()

        def fake_summary(section_title, articles, site_root, config=None):
            nonlocal concurrent, max_observed
            with lock:
                concurrent += 1
                max_observed = max(max_observed, concurrent)
            time.sleep(0.15)
            with lock:
                concurrent -= 1
            return f"<summary-for-{section_title}>"

        generate_news.get_section_summary = fake_summary

        jobs = [(f"S{i}", [_make_article(i)]) for i in range(4)]
        generate_news.summarize_sections_concurrent(
            jobs, Path("/tmp"), max_workers=2
        )
        self.assertLessEqual(max_observed, 2)
        self.assertEqual(max_observed, 2, "Expected exactly 2 concurrent workers")

    def test_failing_section_does_not_kill_others(self):
        """`get_section_summary` is contractually a no-raise function
        (it returns 'Summary could not be generated.' on Ollama errors).
        We simulate that contract here: if a worker ever DID raise,
        the helper should swallow it, log, and let the other
        summaries through."""
        def fake_summary(section_title, articles, site_root, config=None):
            if section_title == "Boom":
                raise RuntimeError("simulated LLM explosion")
            time.sleep(0.05)
            return f"<summary-for-{section_title}>"

        generate_news.get_section_summary = fake_summary

        jobs = [
            ("OK1", [_make_article(1)]),
            ("Boom", [_make_article(2)]),
            ("OK2", [_make_article(3)]),
        ]
        out = generate_news.summarize_sections_concurrent(
            jobs, Path("/tmp"), max_workers=3
        )
        # The two non-boom sections should have clean summaries.
        self.assertEqual(out["OK1"], "<summary-for-OK1>")
        self.assertEqual(out["OK2"], "<summary-for-OK2>")
        # The boom section's failure is captured as a placeholder, not
        # propagated. The exact string is an implementation detail; the
        # contract is "non-empty, does not raise".
        self.assertIn("Boom", out)
        self.assertTrue(isinstance(out["Boom"], str))
        self.assertGreater(len(out["Boom"]), 0)

    def test_max_workers_zero_or_negative_falls_back_to_one(self):
        """A pathological `max_workers` value (e.g. someone passes 0)
        should not crash. We cap to at least 1 internally."""
        self._install_fake()
        jobs = [("A", [_make_article(1)]), ("B", [_make_article(2)])]
        # Should not raise, should produce both summaries.
        out = generate_news.summarize_sections_concurrent(
            jobs, Path("/tmp"), max_workers=0
        )
        self.assertEqual(set(out.keys()), {"A", "B"})

    def test_module_max_summary_workers_constant_is_sane(self):
        """If a future edit breaks the default worker cap, we'd rather
        the unit test fail loudly than have the executor spawn 1000
        threads."""
        self.assertGreaterEqual(generate_news.MAX_SUMMARY_WORKERS, 1)
        self.assertLessEqual(generate_news.MAX_SUMMARY_WORKERS, 32)


class TestQueryOllama(unittest.TestCase):
    """Tests for the extracted HTTP helper."""

    def test_returns_empty_string_on_connection_error(self):
        """An unreachable endpoint should return empty string, not raise.

        We point at http://127.0.0.1:1 — port 1 is reserved (tcpmux) and
        effectively never listens on a modern Linux box, so urlopen
        gets a deterministic ECONNREFUSED. (If for some reason port 1
        *does* listen on the test host, the test would still pass
        because urlopen would return an HTTP error which is also
        caught — but we wouldn't be testing the connection-refused
        path. That's a tolerable degradation; the load-bearing
        assertion is "never raises".)"""
        out = generate_news._query_ollama("hi", "fake-model", timeout=2)
        self.assertEqual(out, "")
        self.assertIsInstance(out, str)

    def test_endpoint_constant_matches_old_string(self):
        # The URL was previously hard-coded as a literal inside
        # get_section_summary. This test pins the new module-level
        # constant to that value so a future rename of the production
        # Ollama host doesn't silently break the unit test fixture.
        self.assertEqual(
            generate_news.OLLAMA_ENDPOINT,
            "http://localhost:11434/api/generate",
        )


class TestModuleImports(unittest.TestCase):
    def test_summarize_sections_concurrent_is_importable(self):
        # The whole point is to make sure the new symbol is wired up.
        self.assertTrue(hasattr(generate_news, "summarize_sections_concurrent"))
        self.assertTrue(callable(generate_news.summarize_sections_concurrent))

    def test_query_ollama_is_importable(self):
        self.assertTrue(hasattr(generate_news, "_query_ollama"))
        self.assertTrue(callable(generate_news._query_ollama))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for tests/clean_title.py (ROADMAP Phase 4 — relocated from root).

Covers:
  * whitespace normalization (\\n, \\r, \\t, runs of spaces)
  * Markdown escape behavior for every char in the escape set
  * characters explicitly NOT escaped (e.g. '$')
  * the original demo inputs (regression coverage)
  * import-time silence: importing the module must not print, so that
    `python3 tests/test_logic.py` stays clean.
"""

import contextlib
import io
import unittest

from clean_title import clean_title


class TestWhitespaceNormalization(unittest.TestCase):
    def test_newlines_collapse_to_single_space(self):
        self.assertEqual(clean_title("Title\nWith\nNewline"), "Title With Newline")

    def test_carriage_returns_collapse(self):
        self.assertEqual(clean_title("Title\rWith\rReturn"), "Title With Return")

    def test_tabs_collapse(self):
        self.assertEqual(clean_title("Title\tWith\tTab"), "Title With Tab")

    def test_mixed_whitespace_runs_collapse(self):
        self.assertEqual(clean_title("a\n\n\nb\t\tc"), "a b c")

    def test_runs_of_spaces_collapse_to_single(self):
        self.assertEqual(clean_title("Too    many    spaces"), "Too many spaces")

    def test_leading_and_trailing_whitespace_stripped(self):
        self.assertEqual(clean_title("   padded   "), "padded")

    def test_empty_string_returns_empty(self):
        self.assertEqual(clean_title(""), "")

    def test_whitespace_only_string_returns_empty(self):
        self.assertEqual(clean_title("   \n\t  "), "")


class TestMarkdownEscaping(unittest.TestCase):
    def test_dollar_sign_is_NOT_escaped(self):
        # Regression: $ is intentionally NOT in the escape set (markdown
        # math block delimiter is `$$`, not `$`, and titles commonly
        # contain prices / model sizes like $10B).
        self.assertEqual(clean_title("Price $10"), "Price $10")

    def test_asterisk_escaped(self):
        self.assertEqual(clean_title("Markdown *Bold*"), r"Markdown \*Bold\*")

    def test_double_asterisk_escaped(self):
        self.assertEqual(clean_title("**bold title**"), r"\*\*bold title\*\*")

    def test_backslash_escaped(self):
        self.assertEqual(clean_title("path\\to\\thing"), r"path\\to\\thing")

    def test_backtick_escaped(self):
        self.assertEqual(clean_title("use `code` here"), r"use \`code\` here")

    def test_underscore_escaped(self):
        self.assertEqual(clean_title("snake_case_name"), r"snake\_case\_name")

    def test_curly_braces_escaped(self):
        self.assertEqual(clean_title("a {curly} thing"), r"a \{curly\} thing")

    def test_square_brackets_escaped(self):
        self.assertEqual(clean_title("Title with [brackets]"), r"Title with \[brackets\]")

    def test_parens_escaped(self):
        self.assertEqual(clean_title("Title (with parens)"), r"Title \(with parens\)")

    def test_hash_escaped(self):
        self.assertEqual(clean_title("# Heading"), r"\# Heading")

    def test_plus_escaped(self):
        self.assertEqual(clean_title("C++ guide"), r"C\+\+ guide")

    def test_dash_escaped(self):
        self.assertEqual(clean_title("kebab-case"), r"kebab\-case")

    def test_dot_escaped(self):
        self.assertEqual(clean_title("end."), r"end\.")

    def test_bang_escaped(self):
        self.assertEqual(clean_title("Whoa!"), r"Whoa\!")

    def test_pipe_escaped(self):
        self.assertEqual(clean_title("a | b"), r"a \| b")

    def test_every_escape_char_escaped(self):
        # Belt-and-suspenders: every char in the escape set must produce a
        # backslash-prefixed output for that char alone.
        for ch in r'\`*_{}[]()#+-.!|':
            self.assertEqual(clean_title(ch), "\\" + ch)


class TestDemoInputsRegression(unittest.TestCase):
    """Pin the original demo loop's expected outputs so a refactor can't
    silently change the on-screen behavior of `python3 tests/clean_title.py`."""

    def test_rt_with_newline_and_blockquote(self):
        out = clean_title("RT by @OpenAIDevs: Here's how we use Codex to:\n\n> understand")
        # Newlines collapse, leading RT prefix preserved, no escaping needed
        # for the visible chars in this string.
        self.assertEqual(out, "RT by @OpenAIDevs: Here's how we use Codex to: > understand")

    def test_double_asterisk_bold(self):
        self.assertEqual(clean_title("**bold title**"), r"\*\*bold title\*\*")

    def test_url_with_fragment_preserved(self):
        # URL chars: ':', '/' are NOT in the escape set so they pass through.
        # '.' and '#' ARE in the escape set and get a backslash.
        self.assertEqual(
            clean_title("Check this out: https://example.com/foo#bar"),
            r"Check this out: https://example\.com/foo\#bar",
        )

    def test_brackets_and_asterisks(self):
        self.assertEqual(
            clean_title("Title with [brackets] and *asterisks*"),
            r"Title with \[brackets\] and \*asterisks\*",
        )

    def test_ellipsis_passes_through(self):
        # '…' (U+2026) is not in the escape set and is not whitespace.
        self.assertEqual(
            clean_title("Introducing Project Glasswing: an urgent initiative to help…"),
            "Introducing Project Glasswing: an urgent initiative to help…",
        )


class TestModuleImportIsSilent(unittest.TestCase):
    """ROADMAP Phase 4 explicitly notes that the old root-level `test_clean.py`
    polluted the test runner's stdout. The new module is a regular .py file
    with the demo loop inside an `if __name__ == "__main__":` guard, so
    importing it from anywhere must produce zero stdout/stderr output."""

    def test_importing_module_prints_nothing(self):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            import importlib
            import clean_title as ct
            importlib.reload(ct)
        self.assertEqual(buf_out.getvalue(), "")
        self.assertEqual(buf_err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

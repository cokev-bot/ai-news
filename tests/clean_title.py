"""Title cleaning helper used by the test suite.

Originally lived in the repo root as ``test_clean.py`` (June 2026 cleanup,
ROADMAP Phase 4). Moved here to follow the AGENTS.md "tests live in tests/"
convention. The function escapes Markdown-special characters and normalizes
whitespace so titles render correctly inside Jekyll posts.
"""

import re


def clean_title(title):
    text = re.sub(r'[\n\r\t]+', ' ', title).strip()
    text = re.sub(r' {2,}', ' ', text)
    escape_chars = r'\`*_{}[]()#+-.!|'
    for ch in escape_chars:
        text = text.replace(ch, '\\' + ch)
    return text


if __name__ == "__main__":
    # Manual sanity-check loop. Kept opt-in via the __main__ guard so
    # `python3 -m unittest` / `python3 tests/test_logic.py` never prints
    # this demo output. Invoked directly with:
    #   PYTHONPATH=. python3 tests/clean_title.py
    tests = [
        "RT by @OpenAIDevs: Here's how we use Codex to:\n\n> understand",
        '**bold title**',
        'Check this out: https://example.com/foo#bar',
        'Title with [brackets] and *asterisks*',
        'Introducing Project Glasswing: an urgent initiative to help…',
    ]
    for t in tests:
        print(f'INPUT:  {repr(t[:60])}')
        print(f'OUTPUT: {repr(clean_title(t)[:70])}')
        print()

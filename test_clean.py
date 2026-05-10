import re

def clean_title(title):
    text = re.sub(r'[\n\r\t]+', ' ', title).strip()
    text = re.sub(r' {2,}', ' ', text)
    escape_chars = r'\`*_{}[]()#+-.!|'
    for ch in escape_chars:
        text = text.replace(ch, '\\' + ch)
    return text

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

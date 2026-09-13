#!/usr/bin/env python3
"""Build a public "This Week" page (/week/) for the AI News site.

The most valuable editorial content is the LLM-synthesized "The Big Picture"
paragraph, but it is locked inside individual edition posts. This script walks
the most recent edition posts on disk, extracts each post's Big Picture
paragraph, and emits a static Jekyll page (front matter + timeline) at
``<site_root>/week.html`` that renders them in reverse-chronological order.

Uses the same "front matter, no Jekyll plugin" shape as
``tools/build_source_status.py`` so it survives GitHub Pages' legacy build
(custom ``_plugins/`` generators do not run there).

Reads:
  _posts/*.html   - edition posts, parsed for title/date + Big Picture text

Writes:
  <site_root>/week.html

Usage:
    python3 tools/build_week_page.py [SITE_ROOT] [--days N]

Exits 0 on success. Never raises on missing/garbled posts: a post with no
parseable Big Picture is simply skipped.
"""

from __future__ import annotations

import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SITE_ROOT_DEFAULT = str(Path(__file__).resolve().parent.parent)
OUTPUT_FILE = "week.html"
DEFAULT_DAYS = 7
DEFAULT_BASEURL = "/ai-news"

# The Big Picture block is rendered as:
#   <h3 ...>🌍 The Big Picture</h3>
#   [<div class="audio-player">...</div>]
#   <p>...</p>
BIG_PICTURE_RE = re.compile(
    r'<h3[^>]*>\s*🌍\s*The Big Picture\s*</h3>.*?<p>(.*?)</p>',
    re.DOTALL | re.IGNORECASE,
)

# The Big Picture audio player carries a <source src="/ai-news/assets/audio/...">
# for the big-picture.mp3 summary. Extract that src so the week page can reuse
# the exact same (baseurl-prefixed) audio file the individual page plays.
BIG_PICTURE_AUDIO_RE = re.compile(
    r'<h3[^>]*>\s*🌍\s*The Big Picture\s*</h3>.*?'
    r'<source\s+src="([^"]*big-picture\.mp3)"',
    re.DOTALL | re.IGNORECASE,
)

# Front-matter date line: `date: 2026-09-12 09:49:19 -0700`
DATE_RE = re.compile(r'^date:\s*["\']?(.*?)["\']?\s*$')


def read_baseurl(site_root: Path) -> str:
    """Read the Jekyll `baseurl:` from _config.yml, defaulting to /ai-news."""
    cfg = site_root / "_config.yml"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^baseurl:\s*["\']?(.*?)["\']?\s*$', line.strip())
            if m:
                base = m.group(1).strip().strip('"').strip("'").strip("/")
                return f"/{base}" if base else ""
    return DEFAULT_BASEURL


def _strip_html(text: str) -> str:
    """Strip tags, keep link text, collapse whitespace."""
    clean = re.sub(r'<a[^>]*>([^<]*)</a>', r'\1', text)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    return re.sub(r'\s+', ' ', clean).strip()


def extract_big_picture(post_text: str) -> str:
    """Return the Big Picture paragraph text (HTML stripped), or ""."""
    m = BIG_PICTURE_RE.search(post_text)
    if not m:
        return ""
    return _strip_html(m.group(1))


def extract_big_picture_audio(post_text: str) -> str:
    """Return the Big Picture summary audio src (baseurl-prefixed), or ""."""
    m = BIG_PICTURE_AUDIO_RE.search(post_text)
    return m.group(1) if m else ""


def audio_player_html(audio_src: str, label: str = "Big Picture summary") -> str:
    """Render an <audio> player for a baseurl-prefixed MP3 src."""
    src = html.escape(audio_src, quote=True)
    label_esc = html.escape(label, quote=True)
    return (
        '<div class="audio-player" style="margin: 8px 0;">'
        f'<audio controls preload="none" style="width:100%;max-width:400px;" '
        f'aria-label="Audio summary of {label_esc}">'
        f'<source src="{src}" type="audio/mpeg">'
        f'<a href="{src}">Download {label_esc}</a>'
        '</audio></div>'
    )


def read_post_date(post_text: str) -> datetime | None:
    """Parse the front-matter `date:` into an aware datetime, or None."""
    for line in post_text.splitlines()[:30]:
        m = DATE_RE.match(line.strip())
        if not m:
            continue
        raw = m.group(1).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None
    return None


def collect_entries(posts_dir: Path, *, days: int, now: datetime, baseurl: str) -> list[dict]:
    """Walk edition posts and return Big Picture entries within the window."""
    cutoff = now - timedelta(days=days)
    entries: list[dict] = []
    for post_path in sorted(posts_dir.glob("*.html")):
        try:
            text = post_path.read_text(encoding="utf-8")
        except OSError:
            continue
        dt = read_post_date(text)
        if dt is None:
            continue
        # A naive frontmatter date (no offset) is treated as the build's UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            continue
        bp = extract_big_picture(text)
        if not bp:
            continue
        entries.append({
            "title": _read_title(text, post_path.stem),
            "url": _permalink(dt, post_path.stem, baseurl),
            "date": dt,
            "bp": bp,
            "audio": extract_big_picture_audio(text),
        })
    # Reverse chronological (newest first).
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def _read_title(text: str, fallback: str) -> str:
    m = re.search(r'^title:\s*"(.*?)"\s*$', text, re.MULTILINE)
    return m.group(1) if m else fallback


def _permalink(dt: datetime, stem: str, baseurl: str) -> str:
    """Reconstruct the Jekyll permalink path for an edition post.

    The path must carry the site's baseurl (``/ai-news``) — without it, a link
    like ``/news/2026/09/12/Evening/`` resolves against the domain root and
    404s, which is exactly the bug this fixed.
    """
    edition = stem.rsplit("-", 1)[-1].capitalize()
    day = dt.strftime("%Y/%m/%d")
    base = baseurl.rstrip("/")
    return f"{base}/news/{day}/{edition}/"


def render_page(entries: list[dict], *, days: int, now: datetime) -> str:
    updated = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    items = []
    for e in entries:
        date_label = e["date"].strftime("%B %-d, %Y")
        audio = audio_player_html(e["audio"]) if e.get("audio") else ""
        items.append(
            '<div class="week-entry">\n'
            f'  <time datetime="{e["date"].isoformat()}">{date_label}</time> · '
            f'<a href="{html.escape(e["url"], quote=True)}">{html.escape(e["title"])}</a>\n'
            f'{audio}'
            f'  <p class="week-bp">{html.escape(e["bp"])}</p>\n'
            "</div>"
        )
    body = "\n".join(items) if items else (
        '<p class="week-empty"><em>No editions published in the last '
        f"{days} days.</em></p>"
    )
    return f"""---
layout: page
title: This Week
permalink: /week/
---

<p>The editorial Big Picture from the last {days} days, newest first — a
one-stop scan of what happened this week.</p>

<style>
.week-entry {{ margin-bottom: 1.5em; }}
.week-entry time {{ color: #666; font-size: 0.9em; }}
.week-bp {{ margin: 0.3em 0 0 0; }}
.week-empty {{ color: #666; }}
</style>

{body}

<p><a href="{{{{ '/' | relative_url }}}}">&larr; Home</a> · <a href="{{{{ '/now/' | relative_url }}}}">Today</a></p>
"""


def write_page(site_root: Path, content: str) -> Path:
    out = site_root / OUTPUT_FILE
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(out)
    return out


def build(site_root: Path, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> Path:
    if now is None:
        now = datetime.now(timezone.utc)
    posts_dir = site_root / "_posts"
    baseurl = read_baseurl(site_root)
    entries = collect_entries(posts_dir, days=days, now=now, baseurl=baseurl)
    return write_page(site_root, render_page(entries, days=days, now=now))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    site_root = Path(argv[1]).resolve() if len(argv) > 1 else Path(SITE_ROOT_DEFAULT).resolve()
    days = DEFAULT_DAYS
    if "--days" in argv:
        i = argv.index("--days")
        if i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                days = DEFAULT_DAYS
    if not site_root.exists():
        print(f"Site root does not exist: {site_root}", file=sys.stderr)
        return 1
    out = build(site_root, days=days)
    print(f"Wrote week page → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

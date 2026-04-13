#!/usr/bin/env python3
"""Generate AI news digest posts from RSS feeds. Tracks state to only show new articles."""

import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import json
import os
from datetime import datetime, timezone
from pathlib import Path

FEEDS = {
    "OpenAI News": "https://openai.com/news/rss.xml",
    "Gemini News": "https://blog.google/products-and-platforms/products/gemini/rss/",
}

STATE_FILE = Path(__file__).parent / ".news_state.json"
MAX_AGE_DAYS = 7


def parse_date(date_str: str) -> datetime | None:
    """Parse various RSS date formats."""
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def fetch_feed(name: str, url: str) -> list[dict]:
    """Fetch and parse an RSS feed, returning list of article dicts."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AI-News-Digest/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [!] Failed to fetch {name}: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"  [!] Failed to parse {name}: {e}")
        return []

    articles = []
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (MAX_AGE_DAYS * 86400)

    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
        pub_str = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

        if len(desc) > 300:
            desc = desc[:297].rsplit(" ", 1)[0] + "…"

        pub_dt = parse_date(pub_str)
        article = {
            "title": title, "link": link, "description": desc,
            "pub": pub_str, "pub_dt": pub_dt, "source": name,
        }

        # Skip articles older than MAX_AGE_DAYS
        if pub_dt is not None and pub_dt.timestamp() < cutoff:
            continue

        if title and link:
            articles.append(article)

    return articles


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_links": set(), "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps({
        "seen_links": list(state["seen_links"]),
        "last_run": state["last_run"],
    }))


def generate_post(edition: str, site_root: Path):
    """Fetch feeds, filter new articles, build a Jekyll post."""
    print(f"\n📰 Generating {edition} edition...")
    state = load_state()
    seen = state["seen_links"]
    all_new = []

    for name, url in FEEDS.items():
        print(f"  → Fetching {name}…")
        articles = fetch_feed(name, url)
        new_articles = [a for a in articles if a["link"] not in seen]
        print(f"    Found {len(articles)} recent, {len(new_articles)} new")
        all_new.extend(new_articles)

    if not all_new:
        print("  ✗ No new articles, skipping edition.")
        return False

    # Record these articles so we don't repeat them
    for a in all_new:
        seen.add(a["link"])
    state["seen_links"] = seen
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # Sort: Gemini first, then OpenAI, alphabetically within
    all_new.sort(key=lambda a: (a["source"], a["title"]))

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{date_str}-{edition.lower()}.markdown"
    filepath = site_root / "_posts" / filename

    header_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "---",
        "layout: post",
        f'title: "AI News Digest — {edition} Edition"',
        f'date: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")}',
        "categories: news digest",
        "---",
        "",
        f"## 🤖 AI News — {edition} Edition · {header_date}",
        "",
        "This digest aggregates the latest AI news from OpenAI and Google Gemini.",
        "",
    ]

    current_source = None
    for art in all_new:
        if art["source"] != current_source:
            current_source = art["source"]
            lines.append(f"### {current_source}")
            lines.append("")
        lines.append(f"**[{art['title']}]({art['link']})**")
        if art["description"]:
            lines.append(f"{art['description']}")
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ Saved: {filepath}")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: generate_news.py <edition> <site-root>")
        sys.exit(1)
    edition = sys.argv[1]
    site_root = Path(sys.argv[2]).resolve()
    success = generate_post(edition, site_root)
    sys.exit(0 if success else 1)

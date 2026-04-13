#!/usr/bin/env python3
"""Generate AI news digest posts from RSS feeds.
No cross-edition history — duplicates only detected within the same edition
using title + description similarity (Jaccard, word-level).
"""

import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Nitter RSS feeds for AI news accounts
FEEDS = {
    # OpenAI
    "OpenAI":         "https://nitter.net/OpenAI/rss",
    "OpenAIDevs":     "https://nitter.net/OpenAIDevs/rss",
    "OpenAINewsroom": "https://nitter.net/OpenAINewsroom/rss",
    # Google
    "GoogleAI":       "https://nitter.net/GoogleAI/rss",
    "GoogleAIStudio": "https://nitter.net/GoogleAIStudio/rss",
    "googleaidevs":   "https://nitter.net/googleaidevs/rss",
    "GeminiApp":      "https://nitter.net/GeminiApp/rss",
    "NotebookLM":     "https://nitter.net/NotebookLM/rss",
    # Anthropic
    "AnthropicAI":    "https://nitter.net/AnthropicAI/rss",
    "claudeai":       "https://nitter.net/claudeai/rss",
    "antigravity":    "https://nitter.net/antigravity/rss",
    # Mistral
    "MistralAI":      "https://nitter.net/MistralAI/rss",
    "MistralDevs":    "https://nitter.net/MistralDevs/rss",
    "mistralvibe":    "https://nitter.net/mistralvibe/rss",
    # Ollama
    "ollama":         "https://nitter.net/ollama/rss",
    # ChatGPT
    "ChatGPTapp":     "https://nitter.net/ChatGPTapp/rss",
    # Misc / influencers
    "bcherny":        "https://nitter.net/bcherny/rss",
    "DarioAmodei":    "https://nitter.net/DarioAmodei/rss",
}

MAX_AGE_DAYS = 7
MAX_ITEMS_PER_SOURCE = 20
TITLE_SIM_THRESHOLD = 0.40  # Jaccard similarity for duplicate detection


def text_similarity(a: str, b: str) -> float:
    """Jaccard similarity between two strings (word-level)."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union > 0 else 0.0


def is_duplicate(new_art: dict, seen: list[dict]) -> bool:
    """Return True if new_art is a duplicate of any article already in seen.

    Uses Jaccard similarity on both title and description.
    Two articles are considered duplicates if either:
      - title similarity >= TITLE_SIM_THRESHOLD
      - description similarity >= TITLE_SIM_THRESHOLD
    Also uses exact match on key phrases like model version numbers
    (e.g., "Claude 4.5", "GPT-5") to catch semantically same announcements.
    """
    new_title = new_art["title"]
    new_desc  = new_art.get("description", "")
    new_lower = new_title.lower()

    for existing in seen:
        # Jaccard on title and description
        title_sim = text_similarity(new_title, existing["title"])
        desc_sim  = text_similarity(new_desc, existing.get("description", ""))
        if title_sim >= TITLE_SIM_THRESHOLD or desc_sim >= TITLE_SIM_THRESHOLD:
            return True

        # Extra check: if both titles share a "model version" pattern
        # (e.g. "claude 4.5" or "gpt-5"), treat as dup
        model_pattern = re.compile(
            r"(claude[-\s]\d[.\d]*|gpt[-\s]\d[.\d]*|\d+\.\d+[a-z]?)", re.I
        )
        new_models = set(model_pattern.findall(new_lower))
        old_models = set(model_pattern.findall(existing["title"].lower()))
        if new_models and old_models and new_models == old_models:
            return True

    return False


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
    """Fetch and parse an RSS feed, returning a list of article dicts."""
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

    for item in root.findall(".//item")[:MAX_ITEMS_PER_SOURCE]:
        title_el = item.find("title")
        link_el  = item.find("link")
        desc_el  = item.find("description")
        pub_el   = item.find("pubDate")

        title   = title_el.text.strip() if title_el is not None and title_el.text else ""
        link    = link_el.text.strip()  if link_el  is not None and link_el.text  else ""
        desc    = desc_el.text.strip()  if desc_el  is not None and desc_el.text  else ""
        pub_str = pub_el.text.strip()   if pub_el   is not None and pub_el.text   else ""

        # Strip HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc)
        if len(desc) > 300:
            desc = desc[:297].rsplit(" ", 1)[0] + "…"

        pub_dt = parse_date(pub_str)
        if pub_dt is not None and pub_dt.timestamp() < cutoff:
            continue

        if title and link:
            articles.append({
                "title": title,
                "link": link,
                "description": desc,
                "pub": pub_str,
                "pub_dt": pub_dt,
                "source": name,
            })

    return articles


def generate_post(edition: str, site_root: Path) -> bool:
    """Fetch all feeds, deduplicate within edition, write Jekyll post."""
    print(f"\n📰 Generating {edition} edition...")
    seen_this_run: list[dict] = []  # articles kept for this edition

    for name, url in FEEDS.items():
        print(f"  → {name}…")
        articles = fetch_feed(name, url)
        print(f"    {len(articles)} fetched")
        for a in articles:
            if not is_duplicate(a, seen_this_run):
                seen_this_run.append(a)
                print(f"      + kept: {a['title'][:60]}")
            else:
                print(f"      - dup:  {a['title'][:60]}")

    if not seen_this_run:
        print("  ✗ No new articles, skipping edition.")
        return False

    seen_this_run.sort(key=lambda a: (a["source"], a["title"]))

    date_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename  = f"{date_str}-{edition.lower()}.markdown"
    filepath  = site_root / "_posts" / filename
    header_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    num_sources = len(set(a["source"] for a in seen_this_run))

    lines = [
        "---",
        "layout: post",
        f'title: "AI News Digest — {edition} Edition"',
        f'date: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")}',
        "categories: news digest",
        "---",
        "",
        f"## 🤖 AI News — {edition} Edition · {header_dt}",
        "",
        f"Scanning {len(FEEDS)} sources · {num_sources} accounts posted · {len(seen_this_run)} items",
        "",
    ]

    current_source = None
    for art in seen_this_run:
        if art["source"] != current_source:
            current_source = art["source"]
            lines.append(f"### {current_source}")
            lines.append("")
        lines.append(f"**[{art['title']}]({art['link']})**")
        if art["description"]:
            lines.append(art["description"])
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ Saved {len(seen_this_run)} items → {filepath}")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: generate_news.py <edition> <site-root>")
        sys.exit(1)
    edition   = sys.argv[1]
    site_root = Path(sys.argv[2]).resolve()
    success   = generate_post(edition, site_root)
    sys.exit(0 if success else 1)
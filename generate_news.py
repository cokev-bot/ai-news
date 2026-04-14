#!/usr/bin/env python3
"""Generate AI news digest posts from RSS feeds.

Sections → Subsections → Feeds → Items.
Duplicates detected within the same edition using title + description
similarity (Jaccard, word-level).
"""

import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Structure: Sections → Subsections → Feeds
# Adding a new section/subsection/feed is just adding an entry here.
# ---------------------------------------------------------------------------

NITTER_FEEDS = {
    "AnthropicAI", "claudeai", "antigravity",
    "OpenAI", "OpenAIDevs", "OpenAINewsroom", "ChatGPTapp",
    "GoogleAI", "GoogleAIStudio", "googleaidevs", "GeminiApp", "NotebookLM",
    "MistralAI", "MistralDevs", "mistralvibe",
    "ollama",
    "bcherny", "DarioAmodei",
}

SECTIONS = [
    {
        "title": "News",
        "subsections": [
            {
                "title": "Financial Times",
                "feeds": {
                    "FT AI":           "https://www.ft.com/artificial-intelligence?format=rss",
                    "FT The AI Shift":  "https://www.ft.com/the-ai-shift?format=rss",
                },
            },
        ],
    },
    {
        "title": "Social Media",
        "subsections": [
            {
                "title": "Anthropic",
                "feeds": {
                    "AnthropicAI": "https://nitter.net/AnthropicAI/rss",
                    "claudeai":    "https://nitter.net/claudeai/rss",
                },
            },
            {
                "title": "OpenAI",
                "feeds": {
                    "OpenAI":          "https://nitter.net/OpenAI/rss",
                    "OpenAIDevs":      "https://nitter.net/OpenAIDevs/rss",
                    "OpenAINewsroom":  "https://nitter.net/OpenAINewsroom/rss",
                    "ChatGPTapp":      "https://nitter.net/ChatGPTapp/rss",
                },
            },
            {
                "title": "Google",
                "feeds": {
                    "GoogleAI":        "https://nitter.net/GoogleAI/rss",
                    "GoogleAIStudio":  "https://nitter.net/GoogleAIStudio/rss",
                    "googleaidevs":    "https://nitter.net/googleaidevs/rss",
                    "GeminiApp":       "https://nitter.net/GeminiApp/rss",
                    "NotebookLM":      "https://nitter.net/NotebookLM/rss",
                    "antigravity":     "https://nitter.net/antigravity/rss",
                },
            },
            {
                "title": "Mistral",
                "feeds": {
                    "MistralAI":   "https://nitter.net/MistralAI/rss",
                    "MistralDevs": "https://nitter.net/MistralDevs/rss",
                    "mistralvibe": "https://nitter.net/mistralvibe/rss",
                },
            },
            {
                "title": "Ollama",
                "feeds": {
                    "ollama": "https://nitter.net/ollama/rss",
                },
            },
            {
                "title": "Influencers",
                "feeds": {
                    "bcherny":     "https://nitter.net/bcherny/rss",
                    "DarioAmodei": "https://nitter.net/DarioAmodei/rss",
                },
            },
        ],
    },
]

MAX_AGE_DAYS = 7
MAX_ITEMS_PER_SOURCE = 20
TITLE_SIM_THRESHOLD = 0.40   # Jaccard similarity threshold for duplicate detection


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def text_similarity(a: str, b: str) -> float:
    """Jaccard similarity (word-level) between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union > 0 else 0.0


def load_state(state_path: Path) -> dict:
    """Load persistent state from .news_state.json, creating if absent."""
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            pass
    return {"seen_links": [], "last_run": None}


def save_state(state_path: Path, state: dict) -> None:
    """Write state back to .news_state.json atomically."""
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)


def is_duplicate(new_art: dict, seen: list[dict], seen_links: set[str]) -> bool:
    """Return True if new_art is a near-duplicate of any article in seen
    OR if its link has appeared in any previous edition.
    """
    # Fast path: link already seen in a prior edition
    if new_art["link"] in seen_links:
        return True

    new_title = new_art["title"]
    new_desc  = new_art.get("description", "")
    new_lower = new_title.lower()

    for existing in seen:
        title_sim = text_similarity(new_title, existing["title"])
        desc_sim  = text_similarity(new_desc, existing.get("description", ""))
        if title_sim >= TITLE_SIM_THRESHOLD or desc_sim >= TITLE_SIM_THRESHOLD:
            return True

        model_pattern = re.compile(
            r"(claude[-\s]\d[.\d]*|gpt[-\s]\d[.\d]*|\d+\.\d+[a-z]?)", re.I
        )
        new_models = set(model_pattern.findall(new_lower))
        old_models = set(model_pattern.findall(existing["title"].lower()))
        if new_models and old_models and new_models == old_models:
            return True

    return False


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(date_str: str) -> datetime | None:
    """Parse common RSS date formats."""
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


# ---------------------------------------------------------------------------
# URL linkification
# ---------------------------------------------------------------------------

def linkify_urls(text: str) -> str:
    """Replace bare http(s) URLs in text with <a href> tags.

    Skips URLs already inside existing anchor tags.
    Converts nitter.net href values to x.com.
    """
    def make_link(m: re.Match) -> str:
        url = m.group(0).strip()
        display = url.replace("nitter.net", "x.com")
        return '<a href="{}">{}</a>'.format(display, display)

    def replace_outside_anchors(text: str) -> str:
        parts = re.split(r'(<a[^>]*>.*?</a>)', text, flags=re.DOTALL | re.IGNORECASE)
        result = []
        for part in parts:
            if re.match(r'<a[^>]*>.*?</a>', part, flags=re.DOTALL | re.IGNORECASE):
                # Rewrite href inside this anchor tag from nitter to x
                part = re.sub(
                    r'href="https?://nitter\.net([^"]*)"',
                    lambda m: 'href="https://x.com' + m.group(1) + '"',
                    part,
                    flags=re.IGNORECASE
                )
                result.append(part)
            else:
                result.append(re.sub(r'https?://[^\s<>"\')\]]+', make_link, part))
        return ''.join(result)

    return replace_outside_anchors(text)


# ---------------------------------------------------------------------------
# HTML rendering per item
# ---------------------------------------------------------------------------

def nitter_to_x(link: str) -> str:
    """Convert nitter.net URL to x.com."""
    return link.replace("nitter.net", "x.com")


def render_item(art: dict) -> str:
    """Render a single article as HTML.

    Nitter / Twitter feeds:
        <strong>FeedName</strong>: tweet text <a href="x.com/...">🔗</a>

    News / FT feeds:
        <strong>FeedName</strong>: <a href="...">Title</a>
        Summary text
    """
    feed_name = art["source"]
    link = art["link"]

    if feed_name in NITTER_FEEDS:
        x_link = nitter_to_x(link)
        tweet_text = linkify_urls(art["title"])
        return (
            '<p>'
            '<strong>{}</strong>: {} '
            '<a href="{}">🔗</a>'
            '</p>'
        ).format(feed_name, tweet_text, x_link)
    else:
        # News/FT: title as link, description below
        title_html = (
            '<p>'
            '<strong>{}</strong>: <a href="{}">{}</a>'
            '</p>'
        ).format(feed_name, link, art["title"])
        desc_html = ""
        if art.get("description"):
            desc_html = "<p>{}</p>".format(art["description"])
        return title_html + desc_html


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

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

        # Strip HTML from description
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


# ---------------------------------------------------------------------------
# Post generation
# ---------------------------------------------------------------------------

def generate_post(edition: str, site_root: Path) -> bool:
    """Fetch all feeds, deduplicate within edition and across past editions,
    write Jekyll post, and persist seen links to .news_state.json.
    """
    print(f"\n📰 Generating {edition} edition...")

    state_path = site_root / ".news_state.json"
    state = load_state(state_path)
    seen_links: set[str] = set(state.get("seen_links", []))

    # seen_this_run: all articles kept for this edition (deduplicated)
    seen_this_run: list[dict] = []

    # Track per-subsection so we can report counts
    subsection_articles: dict[str, list[dict]] = {}

    for section in SECTIONS:
        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            subsection_articles[sub_key] = []

            for feed_name, feed_url in subsection["feeds"].items():
                print(f"  → {feed_name}…")
                articles = fetch_feed(feed_name, feed_url)
                print(f"    {len(articles)} fetched")
                for a in articles:
                    if not is_duplicate(a, seen_this_run, seen_links):
                        seen_this_run.append(a)
                        subsection_articles[sub_key].append(a)
                        print(f"      + kept: {a['title'][:60]}")
                    else:
                        print(f"      - dup:  {a['title'][:60]}")

    if not seen_this_run:
        print("  ✗ No new articles, skipping edition.")
        return False

    # Persist all seen links (prior + current) for cross-edition dedup
    all_links = seen_links | {a["link"] for a in seen_this_run}
    state["seen_links"] = sorted(all_links)
    save_state(state_path, state)
    print(f"  📡 {len(all_links)} total seen links persisted")

    # Sort each subsection's articles alphabetically by source then title
    for sub_key in subsection_articles:
        subsection_articles[sub_key].sort(key=lambda a: (a["source"], a["title"]))

    date_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename  = f"{date_str}-{edition.lower()}.html"
    filepath  = site_root / "_posts" / filename
    header_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total_feeds = sum(
        len(ss["feeds"])
        for section in SECTIONS
        for ss in section["subsections"]
    )
    num_sources = len(set(a["source"] for a in seen_this_run))

    html_lines = [
        "---",
        "layout: post",
        f'title: "AI News Digest — {edition} Edition"',
        f'date: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")}',
        "categories: news digest",
        "---",
        "",
        "<h2>🤖 AI News — {} Edition · {}</h2>".format(edition, header_dt),
        "<p>Scanning {} feeds · {} accounts posted · {} items</p>".format(
            total_feeds, num_sources, len(seen_this_run)),
        "<hr>",
    ]

    for section in SECTIONS:
        html_lines.append("<h2>{}</h2>".format(section["title"]))

        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            items = subsection_articles[sub_key]
            if not items:
                continue

            html_lines.append("")
            html_lines.append("<h3>{}</h3>".format(subsection["title"]))
            html_lines.append("")
            for art in items:
                html_lines.append(render_item(art))
                html_lines.append("")

    filepath.write_text("\n".join(html_lines), encoding="utf-8")
    print(f"\n  ✓ Saved {len(seen_this_run)} items → {filepath}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: generate_news.py <edition> <site-root>")
        sys.exit(1)
    edition   = sys.argv[1]
    site_root = Path(sys.argv[2]).resolve()
    success   = generate_post(edition, site_root)
    sys.exit(0 if success else 1)

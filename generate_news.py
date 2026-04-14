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

# Nitter RSS feeds for AI news accounts (Twitter/X)
NITTER_FEEDS = {
    # OpenAI
    "OpenAI", "OpenAIDevs", "OpenAINewsroom",
    # Google
    "GoogleAI", "GoogleAIStudio", "googleaidevs", "GeminiApp", "NotebookLM",
    # Anthropic
    "AnthropicAI", "claudeai", "antigravity",
    # Mistral
    "MistralAI", "MistralDevs", "mistralvibe",
    # Ollama
    "ollama",
    # ChatGPT
    "ChatGPTapp",
    # Misc / influencers
    "bcherny", "DarioAmodei",
}

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
    # Financial Times
    "FT AI":             "https://www.ft.com/artificial-intelligence?format=rss",
    "FT The AI Shift":   "https://www.ft.com/the-ai-shift?format=rss",

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


def linkify_urls(text: str) -> str:
    """Turn bare URLs in text into clickable <a href> links.

    Skips URLs already inside HTML anchor tags to avoid nesting.
    Converts nitter.net URLs to x.com.
    """
    def make_link(m: re.Match) -> str:
        url = m.group(0).strip()
        # Normalize nitter → x
        display = url.replace("nitter.net", "x.com")
        return '<a href="{}">{}</a>'.format(display, display)

    # Match URLs but not inside existing <a ...>...</a> tags
    # Split on anchor tags, process URL portions only
    parts = re.split(r'(<a[^>]*>.*?</a>)', text, flags=re.DOTALL | re.IGNORECASE)
    out = []
    for part in parts:
        if re.match(r'<a[^>]*>.*?</a>', part, flags=re.DOTALL | re.IGNORECASE):
            out.append(part)  # leave existing links alone
        else:
            out.append(re.sub(r'https?://[^\s<>"\')]+', make_link, part))
    return ''.join(out)


def clean_title(title: str) -> str:
    """Normalize title for clean markdown rendering.

    - Replace newlines/multiple spaces with a single space
    - Escape markdown special characters so titles don't break formatting
    """
    # Collapse whitespace and strip leading/trailing
    text = re.sub(r"[\n\r\t]+", " ", title).strip()
    text = re.sub(r" {2,}", " ", text)

    # Escape characters that have special meaning in markdown
    escape_chars = r"\`*_{}[\]()#+-.!|"
    for ch in escape_chars:
        text = text.replace(ch, "\\" + ch)

    return text


def nitter_to_x(link: str) -> str:
    """Convert a nitter.net URL to x.com URL."""
    return link.replace("nitter.net", "x.com")


def format_article_html(art: dict) -> str:
    """Return the HTML for a single article, tailored to source type.

    - Nitter (Twitter): show tweet text as a blockquote-style paragraph,
      with 🔗 link to the x.com post at the end. URLs in tweet text
      are auto-linked.
    - FT / other: title as a bold link + description paragraph.
    """
    if art["source"] in NITTER_FEEDS:
        x_link = nitter_to_x(art["link"])
        # Tweet text, with any bare URLs turned into links
        tweet_text = linkify_urls(art["title"])
        return "<p>{} <a href=\"{}\">🔗</a></p>".format(tweet_text, x_link)
    else:
        # FT / news source: title as bold link + optional description
        lines = [
            "<p><strong><a href=\"{}\">{}</a></strong></p>".format(
                art["link"], art["title"])
        ]
        if art.get("description"):
            lines.append("<p>{}</p>".format(art["description"]))
        return "\n".join(lines)


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
    """Fetch all feeds, deduplicate within edition, write Jekyll post as HTML.

    Uses .html extension with raw HTML body to bypass Kramdown's link+emphasis
    parsing bug (GFM prohibits **bold [text](url) bold** constructs).
    """
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
    filename  = f"{date_str}-{edition.lower()}.html"
    filepath  = site_root / "_posts" / filename
    header_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    num_sources = len(set(a["source"] for a in seen_this_run))

    # Build HTML content ( Jekyll processes .html with layout=post )
    html_lines = [
        "---",
        "layout: post",
        f'title: "AI News Digest — {edition} Edition"',
        f'date: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")}',
        "categories: news digest",
        "---",
        "",
        "<h2>🤖 AI News — {} Edition · {}</h2>".format(edition, header_dt),
        "<p>Scanning {} sources · {} accounts posted · {} items</p>".format(
            len(FEEDS), num_sources, len(seen_this_run)),
        "<hr>",
    ]

    current_source = None
    for art in seen_this_run:
        if art["source"] != current_source:
            current_source = art["source"]
            html_lines.append("<h3>{}</h3>".format(current_source))
            html_lines.append("")
        html_lines.append(format_article_html(art))
        html_lines.append("")

    filepath.write_text("\n".join(html_lines), encoding="utf-8")
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
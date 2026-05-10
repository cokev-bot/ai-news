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
            data = json.loads(state_path.read_text())
            # Migrate legacy list format to dict format
            seen = data.get("seen_links", {})
            if isinstance(seen, list):
                seen = {link: {"edition": "2026-04-14-morning", "feed": "Unknown", "title": link, "description": ""} for link in seen}
                data["seen_links"] = seen
                save_state(state_path, data)
            # Migrate legacy string-value format ({"link": "edition"}) to full dict
            elif isinstance(seen, dict) and seen and isinstance(next(iter(seen.values())), str):
                migrated = {}
                for link, ed in seen.items():
                    migrated[link] = {"edition": ed, "feed": "Unknown", "title": link, "description": ""}
                data["seen_links"] = migrated
                save_state(state_path, data)
            # Migrate partial dict entries missing feed/title/description keys
            elif isinstance(seen, dict) and seen:
                first_val = next(iter(seen.values()))
                if isinstance(first_val, dict) and "feed" not in first_val:
                    for link, info in seen.items():
                        info.setdefault("feed", "Unknown")
                        info.setdefault("title", link)
                        info.setdefault("description", "")
                    save_state(state_path, data)
            return data
        except Exception:
            pass
    return {"seen_links": {}, "last_run": None}


def save_state(state_path: Path, state: dict) -> None:
    """Write state back to .news_state.json atomically."""
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)


def is_duplicate(new_art: dict, seen: list[dict], seen_links: dict[str, dict]) -> bool:
    """Return True if new_art is a near-duplicate of any article in seen
    OR if its link has appeared in any previous edition.
    """
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
# LLM Summarization
# ---------------------------------------------------------------------------

def linkify_summary(text: str, articles: list[dict]) -> str:
    """Replace (Source: ID, ...) citations in LLM summary with HTML links."""
    def replace_group(match):
        # The group content inside the parentheses
        group_content = match.group(1)
        # Split by comma to handle multiple citations in one pair of parens
        parts = group_content.split(',')
        processed_parts = []
        
        for part in parts:
            part = part.strip()
            # Match "Source Name: ID"
            sub_match = re.search(r'([^:]+):\s*(\d+)', part)
            if sub_match:
                source_name = sub_match.group(1).strip()
                try:
                    article_id = int(sub_match.group(2))
                    if 1 <= article_id <= len(articles):
                        art = articles[article_id - 1]
                        link = art["link"]
                        if art["source"] in NITTER_FEEDS:
                            link = nitter_to_x(link)
                        processed_parts.append(f'<a href="{link}">{source_name}</a>')
                except (ValueError, IndexError):
                    pass
            else:
                # If it doesn't match Source:ID, just put the text back
                processed_parts.append(part)
        
        return '(' + ', '.join(processed_parts) + ')'

    # Match anything inside parentheses: (...)
    return re.sub(r'\(([^)]+)\)', replace_group, text)


def get_section_summary(section_title: str, articles: list[dict], site_root: Path) -> str:
    """Use a local Ollama instance to summarize the articles in a section."""
    if not articles:
        return "No significant updates in this section."

    prompt_path = site_root / "summary_prompt.txt"
    if not prompt_path.exists():
        return "Summary unavailable (prompt file missing)."

    prompt_base = prompt_path.read_text(encoding="utf-8")
    
    # Format the articles into a clear numbered list for the LLM
    content_lines = []
    for i, a in enumerate(articles, 1):
        line = f"{i}. [{a['source']}] {a['title']}"
        if a.get("description"):
            line += f": {a['description']}"
        content_lines.append(line)
    
    full_prompt = f"{prompt_base}\n\nSection: {section_title}\nArticles:\n" + "\n".join(content_lines)

    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps({
                "model": "gemma4:31b-cloud",
                "prompt": full_prompt,
                "stream": False
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data.get("response", "").strip()
    except Exception as e:
        print(f"  [!] LLM summary failed for {section_title}: {e}")
        return "Summary could not be generated."


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

def generate_post(edition: str, site_root: Path, republish: bool = False) -> bool:
    """Fetch all feeds, deduplicate within edition and across past editions,
    write Jekyll post, and persist seen links to .news_state.json.

    Args:
        edition: Full edition name e.g. "2026-04-14-evening"
        site_root: Path to the AI news site root
        republish: If True, rebuild post from links already stored in
                   .news_state.json for the given edition (no fresh fetch).
    """
    print(f"\n📰 Generating {edition} edition...{(' [REPUBLISH]' if republish else '')}")

    state_path = site_root / ".news_state.json"
    state = load_state(state_path)
    seen_links: dict[str, dict] = state.get("seen_links", {})

    subsection_articles: dict[str, list[dict]] = {}

    if republish:
        # Reconstruct articles directly from state — no feed fetching needed
        for section in SECTIONS:
            for subsection in section["subsections"]:
                subsection_articles[subsection["title"]] = []

        # Build a reverse map: feed_url → feed_name for all feeds
        feed_name_by_url: dict[str, str] = {}
        feed_url_by_name: dict[str, str] = {}
        for section in SECTIONS:
            for subsection in section["subsections"]:
                for feed_name, feed_url in subsection["feeds"].items():
                    feed_name_by_url[feed_url] = feed_name
                    feed_url_by_name[feed_name] = feed_url

        for link, info in seen_links.items():
            if info.get("edition") != edition:
                continue
            # If feed is Unknown, try to look it up by nitter username
            raw_feed = info.get("feed", "Unknown")
            feed_name = raw_feed if raw_feed != "Unknown" else None
            if not feed_name:
                # Try to extract nitter username from the link path
                m = re.search(r"nitter\.net/([^/]+)/", link)
                if m:
                    nitter_user = m.group(1)
                    for fname in feed_url_by_name:
                        if fname.lower() == nitter_user.lower():
                            feed_name = fname
                            break
            if not feed_name:
                feed_name = "Unknown"

            art = {
                "title": info.get("title", link),
                "link": link,
                "description": info.get("description", ""),
                "source": feed_name,
                "pub": "",
                "pub_dt": None,
            }
            # Find which subsection this feed belongs to
            sub_key = None
            for section in SECTIONS:
                for subsection in section["subsections"]:
                    if feed_name in subsection["feeds"]:
                        sub_key = subsection["title"]
                        break
                if sub_key:
                    break
            if sub_key:
                subsection_articles[sub_key].append(art)

        total_items = sum(len(v) for v in subsection_articles.values())
        print(f"  📋 Republished {total_items} items from edition '{edition}'")
        if total_items == 0:
            print(f"  ✗ No links found for edition '{edition}' in state.")
            return False
    else:
        # Fresh run: fetch feeds and deduplicate
        seen_this_run: list[dict] = []

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
            print("  ✗ No articles to publish, skipping edition.")
            return False

        # Persist new links with metadata
        new_entries = {}
        for a in seen_this_run:
            new_entries[a["link"]] = {
                "edition": edition,
                "feed": a["source"],
                "title": a["title"],
                "description": a.get("description", ""),
            }
        all_links = {**seen_links, **new_entries}
        state["seen_links"] = all_links
        save_state(state_path, state)
        print(f"  📡 {len(all_links)} total seen links persisted")

    # Sort each subsection's articles alphabetically by source then title
    for sub_key in subsection_articles:
        subsection_articles[sub_key].sort(key=lambda a: (a["source"], a["title"]))

    filename = f"{edition}.html"
    filepath = site_root / "_posts" / filename
    header_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Derive human-readable edition label from full name (e.g. "Evening" from "2026-04-14-evening")
    edition_label = edition.split("-")[-1].capitalize()

    total_feeds = sum(
        len(ss["feeds"])
        for section in SECTIONS
        for ss in section["subsections"]
    )
    num_sources = len(set(
        a["source"]
        for items in subsection_articles.values()
        for a in items
    ))

    html_lines = [
        "---",
        "layout: post",
        f'title: "AI News Digest — {edition_label} Edition"',
        f'date: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")}',
        "categories: news digest",
        "---",
        "",
        "<h2>🤖 AI News — {} Edition · {}</h2>".format(edition_label, header_dt),
        "<p>Scanning {} feeds · {} accounts posted · {} items</p>".format(
            total_feeds, num_sources, sum(len(v) for v in subsection_articles.values())),
        "<hr>",
    ]

    for section in SECTIONS:
        # 1. Gather all articles in this section for the summary
        section_articles = []
        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            section_articles.extend(subsection_articles.get(sub_key, []))

        # 2. Generate summary using LLM
        print(f"  Summarizing section: {section['title']}...")
        summary_text = get_section_summary(section["title"], section_articles, site_root)
        
        # 3. Linkify the summary text
        summary_html = linkify_summary(summary_text, section_articles)

        # 4. Add section heading and summary to HTML
        html_lines.append("<h2>{}</h2>".format(section["title"]))
        html_lines.append('<p><strong>Summary:</strong> {}</p>'.format(summary_html))
        html_lines.append("")

        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            items = subsection_articles.get(sub_key, [])
            if not items:
                continue

            html_lines.append("")
            html_lines.append("<h3>{}</h3>".format(subsection["title"]))
            html_lines.append("")
            for art in items:
                html_lines.append(render_item(art))
                html_lines.append("")

    filepath.write_text("\n".join(html_lines), encoding="utf-8")
    total_items = sum(len(v) for v in subsection_articles.values())
    print(f"\n  ✓ Saved {total_items} items → {filepath}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: generate_news.py <edition> <site-root> [--republish]")
        print("  edition: Full edition name, e.g. '2026-04-14-evening'")
        print("  site-root: Path to the AI news site root")
        sys.exit(1)
    edition   = sys.argv[1]
    site_root = Path(sys.argv[2]).resolve()
    republish = "--republish" in sys.argv or "-r" in sys.argv
    success   = generate_post(edition, site_root, republish=republish)
    sys.exit(0 if success else 1)

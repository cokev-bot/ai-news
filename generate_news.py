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
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration Constants
# ---------------------------------------------------------------------------

MAX_AGE_DAYS = 7
MAX_ITEMS_PER_SOURCE = 20
TITLE_SIM_THRESHOLD = 0.40   # Jaccard similarity threshold for duplicate detection
LOG_FILE = "generate_news.log"

DEFAULT_CONFIG = {
    "model": "gemma4:31b-cloud",
    "summary_prompt_file": "summary_prompt.txt",
}


def load_config(site_root: Path) -> dict:
    """Load config.json from site root, falling back to DEFAULT_CONFIG."""
    config_path = site_root / "config.json"
    if config_path.exists():
        try:
            with config_path.open(encoding="utf-8") as fh:
                cfg = json.load(fh)
            # Merge with defaults so missing keys still work
            merged = {**DEFAULT_CONFIG, **cfg}
            return merged
        except Exception as e:
            logging.warning(f"Failed to load config.json: {e}. Using defaults.")
    return DEFAULT_CONFIG.copy()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def pacific_now() -> datetime:
    """Return current wall-clock time in America/Los_Angeles, with a real tzinfo.

    Replaces an older pytz-based path whose fallback (`datetime.now(timezone.utc)
    + timedelta(hours=-7)`) silently produced UTC-tagged datetimes — %z then
    formatted as "+0000" instead of "-0700", and %Z as "UTC" instead of "PDT".
    This implementation uses zoneinfo (stdlib since Python 3.9) and always
    returns a datetime with a correct fixed-offset tzinfo.
    """
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles"))


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

def is_nitter_link(url: str) -> bool:
    """Detect if a URL is from Nitter."""
    return "nitter.net" in url

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

    if is_nitter_link(link):
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
        group_content = match.group(1)
        parts = group_content.split(',')
        processed_parts = []
        
        for part in parts:
            part = part.strip()
            sub_match = re.search(r'([^:]+):\s*(\d+)', part)
            if sub_match:
                source_name = sub_match.group(1).strip()
                try:
                    article_id = int(sub_match.group(2))
                    if 1 <= article_id <= len(articles):
                        art = articles[article_id - 1]
                        link = art["link"]
                        if is_nitter_link(link):
                            link = nitter_to_x(link)
                        processed_parts.append(f'<a href="{link}">{source_name}</a>')
                except (ValueError, IndexError):
                    pass
            else:
                processed_parts.append(part)
        
        return '(' + ', '.join(processed_parts) + ')'

    return re.sub(r'\(([^)]+)\)', replace_group, text)


def get_section_summary(section_title: str, articles: list[dict], site_root: Path, config: dict | None = None) -> str:
    """Use a local Ollama instance to summarize the articles in a section."""
    if not articles:
        return "No significant updates in this section."

    cfg = config if config is not None else load_config(site_root)
    prompt_file = cfg.get("summary_prompt_file", DEFAULT_CONFIG["summary_prompt_file"])
    model = cfg.get("model", DEFAULT_CONFIG["model"])

    prompt_path = site_root / prompt_file
    if not prompt_path.exists():
        return "Summary unavailable (prompt file missing)."

    prompt_base = prompt_path.read_text(encoding="utf-8")
    
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
                "model": model,
                "prompt": full_prompt,
                "stream": False
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data.get("response", "").strip()
    except Exception as e:
        logging.error(f"LLM summary failed for {section_title}: {e}")
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
        logging.error(f"Failed to fetch {name}: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logging.error(f"Failed to parse {name}: {e}")
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

    # Load site config (model, prompt file, etc.)
    config = load_config(site_root)

    # LOAD SECTIONS FROM EXTERNAL JSON
    sections_path = site_root / "sections.json"
    if not sections_path.exists():
        print(f"  [!] Error: {sections_path} not found.")
        return False
    try:
        SECTIONS = json.loads(sections_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [!] Error parsing sections.json: {e}")
        return False

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
        logging.info(f" {len(all_links)} total seen links persisted")

    # Sort each subsection's articles alphabetically by source then title
    for sub_key in subsection_articles:
        subsection_articles[sub_key].sort(key=lambda a: (a["source"], a["title"]))

    filename = f"{edition}.html"
    filepath = site_root / "_posts" / filename

    # Use Pacific Time for all display timestamps by default (cron-driven
    # runs at 15:00/20:00/00:00 UTC, where 00:00 UTC = 17:00 PT previous day).
    # Set MANUAL_RUN=1 in the environment for ad-hoc runs; in that mode the
    # filename and frontmatter `date:` use UTC, so the post's permalink
    # (computed by Jekyll from the frontmatter date) matches the filename's
    # date and cannot collide with a cron-driven Evening post from the
    # same UTC day. See tests/test_manual_run.py.
    from zoneinfo import ZoneInfo
    if os.environ.get("MANUAL_RUN") == "1":
        post_now = datetime.now(timezone.utc)
    else:
        post_now = datetime.now(ZoneInfo("America/Los_Angeles"))

    header_dt = post_now.strftime("%Y-%m-%d %H:%M %Z")

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
        f'date: {post_now.strftime("%Y-%m-%d %H:%M:%S %z")}',
        "categories: news digest",
        "---",
        "",
        "<h2>🤖 AI News — {} Edition · {}</h2>".format(edition_label, header_dt),
        "<p>Scanning {} feeds · {} accounts posted · {} items</p>".format(
            total_feeds, num_sources, sum(len(v) for v in subsection_articles.values())),
        "<hr>",
    ]

    # Generate global executive summary across ALL sections
    all_articles = []
    for section in SECTIONS:
        for subsection in section["subsections"]:
            all_articles.extend(subsection_articles.get(subsection["title"], []))

    if all_articles:
        logging.info("Generating global 'The Big Picture' summary...")
        global_prompt_base = "Write a high-level 'The Big Picture' executive summary for this edition. Synthesize the most critical trends and developments across all categories into 1-2 punchy paragraphs. Use the same strict citation format (Source: ID)."
        
        def get_global_summary(articles, site_root, config):
            cfg = config if config is not None else load_config(site_root)
            model = cfg.get("model", DEFAULT_CONFIG["model"])
            
            content_lines = []
            for i, a in enumerate(articles, 1):
                line = f"{i}. [{a['source']}] {a['title']}"
                if a.get("description"):
                    line += f": {a['description']}"
                content_lines.append(line)
            
            full_prompt = f"{global_prompt_base}\n\nArticles:\n" + "\n".join(content_lines)

            try:
                req = urllib.request.Request(
                    "http://localhost:11434/api/generate",
                    data=json.dumps({
                        "model": model,
                        "prompt": full_prompt,
                        "stream": False
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=600) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    return res_data.get("response", "").strip()
            except Exception as e:
                logging.error(f"Global summary failed: {e}")
                return "Global summary could not be generated."

        global_summary_text = get_global_summary(all_articles, site_root, config)
        global_summary_html = linkify_summary(global_summary_text, all_articles)
        
        html_lines.append('<div style="background: #f9f9f9; padding: 15px; border-left: 5px solid #ccc; margin-bottom: 20px;">')
        html_lines.append('  <h3 style="margin-top:0;">🌍 The Big Picture</h3>')
        html_lines.append(f'  <p>{global_summary_html}</p>')
        html_lines.append('</div>')
        html_lines.append("")


    for section in SECTIONS:
        # 1. Gather all articles in this section for the summary
        section_articles = []
        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            section_articles.extend(subsection_articles.get(sub_key, []))

        # Skip empty sections entirely
        if not section_articles:
            continue

        # 2. Generate summary using LLM
        logging.info(f"Summarizing section: {section['title']}...")
        summary_text = get_section_summary(section["title"], section_articles, site_root, config)
        
        # 3. Linkify the summary text
        summary_html = linkify_summary(summary_text, section_articles)

        # 4. Add section heading and summary to HTML
        html_lines.append("<h2>{}</h2>".format(section["title"]))
        html_lines.append('<p><strong>Summary:</strong> {}</p>'.format(summary_html))
        html_lines.append("")

        # 5. Build collapsible subsections
        subsections_html_lines = []
        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            items = subsection_articles.get(sub_key, [])
            if not items:
                continue

            subsections_html_lines.append("")
            subsections_html_lines.append("<h3>{}</h3>".format(subsection["title"]))
            subsections_html_lines.append("")
            for art in items:
                subsections_html_lines.append(render_item(art))
                subsections_html_lines.append("")

        if subsections_html_lines:
            html_lines.append('<details>')
            html_lines.append('  <summary><strong>Subsections</strong></summary>')
            html_lines.extend(subsections_html_lines)
            html_lines.append('</details>')
            html_lines.append("")

    filepath.write_text("\n".join(html_lines), encoding="utf-8")
    total_items = sum(len(v) for v in subsection_articles.values())
    logging.info(f"Saved {total_items} items → {filepath}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: generate_news.py <edition> <site-root>")
        print("  edition: Full edition name, e.g. '2026-04-14-evening'")
        print("  site-root: Path to the AI news site root")
        print("  Auto-detects republish if edition already exists in .news_state.json")
        sys.exit(1)
    edition   = sys.argv[1]
    site_root = Path(sys.argv[2]).resolve()

    # Auto-detect republish if edition already exists in state
    state_path = site_root / ".news_state.json"
    state = load_state(state_path)
    seen_links = state.get("seen_links", {})
    republish = any(info.get("edition") == edition for info in seen_links.values())
    if republish:
        print(f"  📋 Edition '{edition}' found in state — republishing.")

    success   = generate_post(edition, site_root, republish=republish)
    sys.exit(0 if success else 1)

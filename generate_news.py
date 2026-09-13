#!/usr/bin/env python3
"""Generate AI news digest posts from RSS feeds.

Sections → Subsections → Feeds → Items.
Duplicates detected within the same edition using title + description
similarity (Jaccard, word-level).
"""

import json
import time
import socket
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import re
import logging
import logging.handlers
import math
import shutil
import html as html_module
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration Constants
# ---------------------------------------------------------------------------

MAX_AGE_DAYS = 7
MAX_HISTORY_DAYS = 60
MAX_ITEMS_PER_SOURCE = 20
TITLE_SIM_THRESHOLD = 0.40   # Jaccard similarity threshold for duplicate detection
# Stories re-reported by another source within this window are considered
# the same story even if the link differs. Catches "breaking" rewrites
# 12h later that the per-edition seen-list misses. Older re-reports are
# allowed through (treated as fresh coverage).
CROSS_EDITION_DEDUP_HOURS = 24
DEFAULT_TIMEZONE = "America/Los_Angeles"
LOG_FILE = "generate_news.log"
# Cap concurrent LLM section-summary workers. Each one holds a long-lived
# HTTP request to the local Ollama instance (up to 600s). Capping at the
# number of top-level sections keeps the wall-clock bounded by the slowest
# section while not overloading the local model server.
MAX_SUMMARY_WORKERS = 6
# Cap concurrent feed-fetch workers. Separate pool from the LLM summary
# workers so slow feeds don't starve summaries (and vice-versa). 10 is
# enough to overlap 31 feeds (typical config) without hammering hosts.
MAX_FEED_WORKERS = 10
# User-Agent for the xcancel RSS mirror. xcancel gates /rss on a per-reader
# allowlist (it returns a 1971-dated "not yet whitelisted" placeholder to
# everything else) and this is the reader identity the mirror serves real
# content to. Do not "modernise" this string — most UAs, including the
# pipeline's own, get the placeholder. See _http_get_with_curl().
XCANCEL_UA = "Inoreader/1.0"
OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"
# Average adult reading speed (words per minute) used to estimate how long
# an edition takes to read. 230 wpm is the mid-range consensus figure for
# non-fiction prose; the estimate is deliberately coarse — it only needs to
# separate a 2-minute skim from a 10-minute deep read.
READING_WORDS_PER_MINUTE = 230
# Model-identifier pattern for duplicate detection: "GPT-5", "Claude 4.5",
# "3.2a", etc. Two titles mentioning the *same* model identifier are treated
# as the same story even when the surrounding prose differs ("gpt-5 safety
# committee" vs "Sam Altman on gpt-5 safety").
MODEL_NAME_PATTERN = re.compile(
    r"(claude[-\s]\d[.\d]*|gpt[-\s]\d[.\d]*|\d+\.\d+[a-z]?)", re.I
)

DEFAULT_CONFIG = {
    "model": "gemma4:31b-cloud",
    "summary_prompt_file": "summary_prompt.txt",
    "timezone": DEFAULT_TIMEZONE,
    "tuning": {
        "max_items_per_source": MAX_ITEMS_PER_SOURCE,
        "max_age_days": MAX_AGE_DAYS,
        "title_sim_threshold": TITLE_SIM_THRESHOLD,
        "cross_edition_dedup_hours": CROSS_EDITION_DEDUP_HOURS,
    },
}


def get_tuning(site_root: Path) -> dict:
    """Load tuning overrides from config.json, merged with DEFAULT_CONFIG defaults.

    Returns a dict with keys: max_items_per_source, max_age_days,
    title_sim_threshold, cross_edition_dedup_hours.  Missing keys in
    the user's config.json fall back to the values in
    DEFAULT_CONFIG['tuning'].
    """
    cfg = load_config(site_root)
    defaults = DEFAULT_CONFIG["tuning"]
    user_tuning = cfg.get("tuning", {})
    return {**defaults, **user_tuning}


def get_timezone(site_root: Path) -> str:
    """Load timezone from config.json, falling back to DEFAULT_TIMEZONE.

    Returns an IANA timezone string (e.g. 'America/Los_Angeles').
    Empty or whitespace-only strings are treated as missing and fall back
    to the default.
    """
    cfg = load_config(site_root)
    tz = cfg.get("timezone", DEFAULT_TIMEZONE)
    return tz.strip() or DEFAULT_TIMEZONE


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
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def pacific_now(site_root: Path | None = None) -> datetime:
    """Return current wall-clock time in the configured timezone, with a real tzinfo.

    The timezone is read from config.json (``timezone`` key, defaulting to
    ``America/Los_Angeles``).  Pass ``site_root`` to resolve the config path;
    if omitted, the default timezone is used.

    Replaces an older pytz-based path whose fallback (`datetime.now(timezone.utc)
    + timedelta(hours=-7)`) silently produced UTC-tagged datetimes — %z then
    formatted as "+0000" instead of "-0700", and %Z as "UTC" instead of "PDT".
    This implementation uses zoneinfo (stdlib since Python 3.9) and always
    returns a datetime with a correct fixed-offset tzinfo.
    """
    from zoneinfo import ZoneInfo
    tz_name = get_timezone(site_root) if site_root else DEFAULT_TIMEZONE
    return datetime.now(ZoneInfo(tz_name))


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


def _share_model_name(a_title: str, b_title: str) -> bool:
    """True if both titles mention the same model identifier (e.g. "GPT-5").

    Extracts every model-name token (``GPT-5``, ``Claude 4.5``, ``3.2a``)
    from each title and returns True only when both mention a non-empty set
    of identifiers *and* those sets are identical. A title with no model
    mention never matches (``new_models``/``old_models`` must both be truthy),
    which keeps ordinary prose from being conflated.
    """
    a_models = set(MODEL_NAME_PATTERN.findall(a_title.lower()))
    b_models = set(MODEL_NAME_PATTERN.findall(b_title.lower()))
    return bool(a_models and b_models and a_models == b_models)


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
            # Migrate entries missing seen_at: treat legacy entries as
            # seen_at=now so they fall outside the 24h cross-edition dedup
            # window and behave exactly as before this Phase 4 change.
            if isinstance(seen, dict):
                needs_seen_at_migration = False
                for info in seen.values():
                    if isinstance(info, dict) and "seen_at" not in info:
                        info["seen_at"] = datetime.now(timezone.utc).isoformat()
                        needs_seen_at_migration = True
                if needs_seen_at_migration:
                    data["seen_links"] = seen
                    save_state(state_path, data)
            # Prune stale entries older than MAX_HISTORY_DAYS.
            # Run after migrations so legacy entries have a seen_at stamp.
            data = prune_state(data)
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


def prune_state(state: dict, max_history_days: int = MAX_HISTORY_DAYS, now: datetime | None = None) -> dict:
    """Remove seen_links entries older than max_history_days.

    This caps the growth of .news_state.json by evicting stale entries.
    Entries without a parseable seen_at timestamp are kept (conservative:
    never lose data we can't reason about).  The one-shot seen_at migration
    in load_state() ensures legacy entries get a timestamp before the first
    prune, so nothing is lost on the first run.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    seen_links = state.get("seen_links", {})
    if not isinstance(seen_links, dict):
        return state

    cutoff_ts = now.timestamp() - (max_history_days * 86400)
    pruned = {}
    for link, info in seen_links.items():
        if not isinstance(info, dict):
            # Malformed entry — keep it rather than silently discard.
            pruned[link] = info
            continue
        seen_at = _parse_seen_at(info)
        if seen_at is None:
            # Unparseable or missing timestamp — keep it (conservative).
            pruned[link] = info
            continue
        if seen_at.timestamp() >= cutoff_ts:
            pruned[link] = info

    state["seen_links"] = pruned
    return state


def _parse_seen_at(info: dict) -> datetime | None:
    """Parse the seen_at ISO timestamp from a seen_links entry.

    Returns None if the field is missing or unparseable. The caller is
    expected to treat None as "old" (fall outside the dedup window) so
    that legacy state and unparseable timestamps behave conservatively
    and never cause a new story to be suppressed.
    """
    raw = info.get("seen_at") if isinstance(info, dict) else None
    if not raw:
        return None
    try:
        # datetime.fromisoformat handles both "...+00:00" and trailing "Z"
        # in Python 3.11+. We normalize the rare "Z" suffix for safety.
        if isinstance(raw, str) and raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def is_duplicate(new_art: dict, seen: list[dict], seen_links: dict[str, dict], *, now: datetime | None = None, title_sim_threshold: float = TITLE_SIM_THRESHOLD, cross_edition_dedup_hours: int = CROSS_EDITION_DEDUP_HOURS) -> bool:
    """Return True if new_art is a near-duplicate of any article in seen
    OR if its link has appeared in any previous edition
    OR if its title closely matches a recent (last cross_edition_dedup_hours)
    seen_links entry from a different link.
    """
    if new_art["link"] in seen_links:
        return True

    new_title = new_art["title"]
    new_desc  = new_art.get("description", "")

    for existing in seen:
        title_sim = text_similarity(new_title, existing["title"])
        desc_sim  = text_similarity(new_desc, existing.get("description", ""))
        if title_sim >= title_sim_threshold or desc_sim >= title_sim_threshold:
            return True

        if _share_model_name(new_title, existing["title"]):
            return True

    # Cross-edition dedup: stories re-reported by another source with a
    # different link within cross_edition_dedup_hours are considered the
    # same story. We only need a title similarity check here because the
    # exact-link check above already handled the "same link" case.
    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now.timestamp() - cross_edition_dedup_hours * 3600
    for link, info in seen_links.items():
        if link == new_art["link"]:
            # Same link, already handled by the membership check above.
            continue
        seen_at = _parse_seen_at(info)
        if seen_at is None:
            # Legacy/unparseable entry: skip the cross-edition check
            # rather than suppress. Conservative = do not lose new stories.
            continue
        if seen_at.timestamp() < window_start:
            continue
        old_title = info.get("title", link) if isinstance(info, dict) else link
        if text_similarity(new_title, old_title) >= title_sim_threshold:
            return True
        # Also catch the model-name pattern across editions.
        if _share_model_name(new_title, old_title):
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


def compute_freshness_tally(items: list[dict], max_age_days: int, now: datetime | None = None, tz_name: str | None = None) -> dict[str, int]:
    """Compute a freshness tally over a list of article dicts.

    Each article dict should have a ``pub_dt`` key whose value is either a
    timezone-aware :class:`datetime` or ``None`` (missing / unparseable).

    Categories:
      - ``fresh``: published within ``max_age_days`` days of *now*.
      - ``stale``: published more than ``max_age_days`` days ago.
      - ``yesterday``: published between 1 and 2 calendar days ago (in the
        configured timezone).

    Items with no ``pub_dt`` are conservatively counted as ``fresh`` (they
    passed the fetch-level age filter, so they are at most ``max_age_days``
    old even if we can't pin down the exact date).

    Args:
        items: List of article dicts (each may have ``pub_dt``).
        max_age_days: Maximum age in days; items older are "stale".
        now: Reference datetime (timezone-aware). Defaults to current time
            in the configured timezone.
        tz_name: IANA timezone name used for calendar-day calculations.
            Defaults to :data:`DEFAULT_TIMEZONE`.

    Returns:
        A dict with keys ``fresh``, ``stale``, and ``yesterday``, each
        mapping to a non-negative integer count.
    """
    from zoneinfo import ZoneInfo

    if tz_name is None:
        tz_name = DEFAULT_TIMEZONE
    tz = ZoneInfo(tz_name)

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    # Compute "today" and "yesterday" as calendar dates in the target tz.
    today = now.date()
    yesterday = today - timedelta(days=1)

    cutoff_ts = (now - timedelta(days=max_age_days)).timestamp()

    fresh = 0
    stale = 0
    yesterday_count = 0

    for art in items:
        pub_dt = art.get("pub_dt")
        if pub_dt is None:
            # No timestamp — conservatively treat as fresh.
            fresh += 1
            continue

        # Ensure pub_dt is timezone-aware for comparison.
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)

        pub_ts = pub_dt.timestamp()

        if pub_ts < cutoff_ts:
            stale += 1
        else:
            fresh += 1

        # Calendar-day check for "yesterday" (between 1 and 2 days ago).
        # Convert pub_dt to the target timezone to get the local date.
        pub_local_date = pub_dt.astimezone(tz).date()
        if pub_local_date == yesterday:
            yesterday_count += 1

    return {"fresh": fresh, "stale": stale, "yesterday": yesterday_count}


def format_freshness_tally(tally: dict[str, int]) -> str:
    """Format a freshness tally dict into a human-readable string fragment.

    Example: ``2 fresh, 0 stale, 1 from yesterday``
    """
    return f"{tally['fresh']} fresh, {tally['stale']} stale, {tally['yesterday']} from yesterday"


# ---------------------------------------------------------------------------
# Reading-time estimate + section count badges
# ---------------------------------------------------------------------------

def count_words(text: str | None) -> int:
    """Count readable words in a string, ignoring any HTML markup.

    ``<a href="...">OpenAI</a>`` counts as one word; tag names and
    attribute values never count. HTML entities are decoded first, so
    ``it&#8217;s`` counts as one word and ``GPT&amp;Gemini`` as two.
    """
    if not text:
        return 0
    plain = html_module.unescape(_strip_html(str(text)))
    # Typographic apostrophes are word-joiners, not separators.
    plain = plain.replace("\u2019", "'")
    return len(re.findall(r"[A-Za-z0-9']+", plain))


def format_reading_time(minutes: int) -> str:
    """Format a minute count as a header fragment, e.g. ``~4 min read``."""
    return f"~{int(minutes)} min read"


def compute_reading_time_minutes(
    section_summaries: dict[str, str] | None = None,
    global_summary_text: str | None = None,
    subsection_articles: dict[str, list[dict]] | None = None,
    words_per_minute: int = READING_WORDS_PER_MINUTE,
) -> int:
    """Estimate how many minutes an edition takes to read.

    Counts every word a reader actually reads: the Big Picture summary,
    every section summary, and the title + description of every item.
    Returns whole minutes (rounded up), or 0 for an edition with no
    readable text at all (so callers can omit the fragment entirely rather
    than print "~0 min read").
    """
    total_words = count_words(global_summary_text)

    for summary in (section_summaries or {}).values():
        total_words += count_words(summary)

    for items in (subsection_articles or {}).values():
        for art in items:
            total_words += count_words(art.get("title", ""))
            total_words += count_words(art.get("description", ""))

    if total_words <= 0:
        return 0

    wpm = words_per_minute if words_per_minute and words_per_minute > 0 else READING_WORDS_PER_MINUTE
    return max(1, math.ceil(total_words / wpm))


def format_section_heading(section_title: str, item_count: int = 0, *, with_id: bool = True) -> str:
    """Render a section heading with an item-count badge, e.g. ``News (3)``.

    The count is wrapped in a ``section-count`` span so the visual weight
    can be tuned by styling without regenerating existing posts. The heading
    carries a slug ``id`` so in-page anchors (and the static JSON API's
    per-section ``url``) have something to land on; pass ``with_id=False``
    for a heading with no badge, which has no anchor to resolve.
    """
    ident = f' id="{_slugify(section_title)}"' if with_id else ""
    return '<h2{}>{} <span class="section-count">({})</span></h2>'.format(
        ident, section_title, int(item_count)
    )


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

def is_retweet(title: str) -> bool:
    """Return True if the title is a retweet-only item from Nitter.

    Nitter renders retweets with titles like 'R to @user: ...'.
    These are low-signal noise that clutter the digest, so we drop them.
    """
    return title.startswith("R to @")


def render_source_pill(feed_name: str, source_urls: dict[str, str]) -> str:
    """Render a source name as a linked pill element.

    If the feed name has a known homepage URL in source_urls, renders as
    an anchor with class ``source-pill``.  Otherwise falls back to a plain
    ``<strong>`` tag (backwards-compatible with feeds not yet in the map).
    """
    url = source_urls.get(feed_name)
    if url:
        return f'<a class="source-pill" href="{url}">{feed_name}</a>'
    return f'<strong>{feed_name}</strong>'


def render_item(art: dict, source_urls: dict[str, str] | None = None) -> str:
    """Render a single article as HTML.

    Nitter / Twitter feeds:
        <a class="source-pill" href="...">FeedName</a>: tweet text <a href="x.com/...">🔗</a>

    News / FT feeds:
        <a class="source-pill" href="...">FeedName</a>: <a href="...">Title</a>
        Summary text
    """
    if source_urls is None:
        source_urls = {}
    # Safety net: skip retweet-only items that slipped through fetch_feed
    if is_retweet(art.get("title", "")):
        return ""

    feed_name = art["source"]
    link = art["link"]
    pill = render_source_pill(feed_name, source_urls)

    if is_nitter_link(link):
        x_link = nitter_to_x(link)
        tweet_text = linkify_urls(art["title"])
        return (
            '<p>'
            '{}: {} '
            '<a href="{}">🔗</a>'
            '</p>'
        ).format(pill, tweet_text, x_link)
    else:
        # News/FT: title as link, description below
        title_html = (
            '<p>'
            '{}: <a href="{}">{}</a>'
            '</p>'
        ).format(pill, link, art["title"])
        desc_html = ""
        if art.get("description"):
            desc_html = "<p>{}</p>".format(art["description"])
        return title_html + desc_html


# ---------------------------------------------------------------------------
# LLM Summarization
# ---------------------------------------------------------------------------

def _read_post_frontmatter_date(post_path: Path) -> datetime | None:
    """Read the ``date:`` field from an existing Jekyll post's frontmatter.

    Returns a timezone-aware datetime, or ``None`` when the file is missing or
    has no parseable date. Used to keep a republish's permalink stable: Jekyll
    derives the post URL from this value, so re-deriving it from "now" silently
    moves a live URL.
    """
    if not post_path.exists():
        return None
    try:
        head = post_path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return None
    for line in head.splitlines():
        line = line.strip()
        if not line.startswith("date:"):
            continue
        raw = line[len("date:"):].strip().strip('"').strip("'")
        # Jekyll frontmatter form: "2026-09-12 09:49:19 -0700"
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            if dt.tzinfo is None:
                return dt.replace(tzinfo=ZoneInfo(get_timezone(post_path.parent.parent)))
            return dt
        return None
    return None


def _article_link(art: dict) -> str:
    """Outbound link for an article, converting dead nitter URLs to x.com."""
    link = art["link"]
    if is_nitter_link(link):
        link = nitter_to_x(link)
    return link


def linkify_summary(text: str, articles: list[dict]) -> str:
    """Replace ``(Source Name: ID, ...)`` citations in an LLM summary with links.

    ``summary_prompt.txt`` mandates ``Source Name: ID`` and the model mostly
    complies, but it also drifts into two collapsed forms where only the first
    index carries the label:

        (Source: 1, 4)          -> one label, trailing bare indices
        (Source, 22, 24, 29)    -> label with no colon at all

    Those produced bare integers with no reference list to resolve them (nine
    resolved links became ``Source</a>, 22, 24, 29`` in a live post on
    2026-09-13). The index -> article mapping is authoritative, so we resolve
    every in-range index and label each link with that article's *own* source
    name. Prose parentheses are left untouched: resolution requires a leading
    non-numeric label followed only by in-range integers, and falls back to the
    original text otherwise, so an unresolvable index never yields an invented
    link.
    """
    def replace_group(match):
        parts = [p.strip() for p in match.group(1).split(',')]
        resolved: list[str] = []
        label: str | None = None

        for i, part in enumerate(parts):
            colon = re.search(r'([^:]+):\s*(\d+)$', part)
            if colon:
                idx = int(colon.group(2))
                if not 1 <= idx <= len(articles):
                    return match.group(0)
                resolved.append(
                    f'<a href="{_article_link(articles[idx - 1])}">'
                    f'{colon.group(1).strip()}</a>'
                )
                label = colon.group(1).strip()
                continue

            if part.isdigit():
                idx = int(part)
                # A bare index is only meaningful under a preceding label;
                # "(1, 2, 3)" is prose, not a citation.
                if label is None or not 1 <= idx <= len(articles):
                    return match.group(0)
                art = articles[idx - 1]
                resolved.append(
                    f'<a href="{_article_link(art)}">'
                    f'{art.get("source", "Source")}</a>'
                )
                continue

            # Non-numeric text: only valid as the leading label of a collapsed
            # citation. Anything else is ordinary prose.
            if label is None and i == 0 and part:
                label = part
                continue
            return match.group(0)

        if not resolved:
            return match.group(0)
        return '(' + ', '.join(resolved) + ')'

    return re.sub(r'\(([^)]+)\)', replace_group, text)


def _query_ollama(prompt: str, model: str, *, timeout: int = 600) -> str:
    """Single HTTP call to the local Ollama /api/generate endpoint.

    Returns the model's response text, or an empty string on any failure
    (caller decides what to do with that — usually log + substitute a
    "summary unavailable" message). Never raises; the network/parse
    failure path is expected to be hit in production when the local model
    is overloaded, and we don't want a single bad call to abort the
    whole edition.

    Extracted from the old in-line body of get_section_summary() so it
    can be reused by summarize_sections_concurrent() without dragging
    prompt-building along.
    """
    try:
        req = urllib.request.Request(
            OLLAMA_ENDPOINT,
            data=json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data.get("response", "").strip()
    except Exception as e:
        logging.error(f"Ollama query failed: {e}")
        return ""


def _format_articles_for_prompt(articles: list[dict]) -> str:
    """Render an article list as numbered prompt lines: ``1. [Source] Title: desc``."""
    content_lines = []
    for i, a in enumerate(articles, 1):
        line = f"{i}. [{a['source']}] {a['title']}"
        if a.get("description"):
            line += f": {a['description']}"
        content_lines.append(line)
    return "\n".join(content_lines)


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

    full_prompt = f"{prompt_base}\n\nSection: {section_title}\nArticles:\n" + _format_articles_for_prompt(articles)

    response = _query_ollama(full_prompt, model)
    return response or "Summary could not be generated."


def get_global_summary(prompt_base: str, articles: list[dict], config: dict | None = None) -> str:
    """Generate 'The Big Picture' executive summary from an article list.

    Unlike ``get_section_summary``, the prompt text is passed directly (there
    is no per-section prompt file); only the model is read from *config*.
    Returns a fallback string on failure rather than raising.
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    model = cfg.get("model", DEFAULT_CONFIG["model"])

    full_prompt = f"{prompt_base}\n\nArticles:\n" + _format_articles_for_prompt(articles)
    response = _query_ollama(full_prompt, model)
    return response or "Global summary could not be generated."


def summarize_sections_concurrent(section_jobs: list[tuple[str, list[dict]]],
                                  site_root: Path,
                                  config: dict | None = None,
                                  max_workers: int = MAX_SUMMARY_WORKERS) -> dict[str, str]:
    """Run get_section_summary() across many sections in parallel.

    `section_jobs` is a list of (section_title, articles) tuples in the
    order they should appear in the final post. We dispatch each tuple
    to its own worker thread and collect the results. The returned dict
    maps section_title -> summary text.

    The function preserves caller's intent that one failing section does
    not abort the others: get_section_summary() already swallows Ollama
    errors and returns a "summary could not be generated" string, so
    worker threads never raise. The wall-clock cost of N serial ~20s
    LLM calls drops to roughly max(per_call_latency) when N <= max_workers.

    `max_workers` is exposed for tests so they can pin it without
    monkey-patching the module-level constant.
    """
    results: dict[str, str] = {}

    if not section_jobs:
        return results

    # cap workers defensively — a typo / future change to MAX_SUMMARY_WORKERS
    # should not cause us to spawn 1000 threads.
    workers = max(1, min(max_workers, len(section_jobs)))
    if workers == 1:
        # Serial fast path: no need to spin up an executor for a single
        # job. Preserves behavior for the tiny single-section case
        # (e.g. a degenerate sections.json in a test fixture).
        for title, articles in section_jobs:
            results[title] = get_section_summary(title, articles, site_root, config)
        return results

    start = time.monotonic()
    logging.info(
        f"Summarizing {len(section_jobs)} sections with up to {workers} workers..."
    )

    # We use submit() + a title-keyed future dict so we can preserve
    # the caller's section order if the caller wants to iterate the
    # returned dict, AND so the slow-first / fast-last case doesn't
    # unnecessarily block collection. The caller already iterates
    # SECTIONS in order using the returned dict, so a non-ordered
    # gather is fine — the order in `results` is not load-bearing.
    future_to_title: dict = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm-sum") as pool:
        for title, articles in section_jobs:
            future_to_title[
                pool.submit(get_section_summary, title, articles, site_root, config)
            ] = title
        for fut in as_completed(future_to_title):
            title = future_to_title[fut]
            try:
                results[title] = fut.result()
            except Exception as e:  # pragma: no cover - defensive
                # get_section_summary is designed not to raise, but if a
                # future from it ever did, we'd rather capture a string
                # than crash the post.
                logging.error(f"Concurrent summary worker for {title!r} raised: {e}")
                results[title] = "Summary could not be generated."

    elapsed = time.monotonic() - start
    logging.info(
        f"Finished {len(section_jobs)} section summaries in {elapsed:.1f}s "
        f"(parallelism ≤ {workers})."
    )
    return results


# ---------------------------------------------------------------------------
# Text-to-Speech
# ---------------------------------------------------------------------------

DEFAULT_TTS_CONFIG = {
    "enabled": True,
    "voice": "en-US-AriaNeural",
    "rate": "+0%",
}

def get_tts_config(site_root: Path) -> dict:
    """Load TTS config from config.json, merged with defaults."""
    cfg = load_config(site_root)
    tts = cfg.get("tts", {})
    return {**DEFAULT_TTS_CONFIG, **tts}

def _strip_html(text: str) -> str:
    """Remove HTML tags from summary text for TTS."""
    clean = re.sub(r'<a[^>]*>([^<]*)</a>', r'\1', text)  # link text only
    clean = re.sub(r'<[^>]+>', ' ', clean)  # strip all other tags
    clean = re.sub(r'\s+', ' ', clean).strip()  # normalize whitespace
    return clean


def _make_description(global_summary_text: str | None, max_chars: int = 160) -> str:
    """Build a YAML-safe, edition-specific meta description from the Big Picture.

    ``jekyll-seo-tag`` emits ``og:description`` and the ``<meta name=description>``
    from ``page.description``; generated posts set none, so every edition's social
    card and SERP snippet shows the generic site tagline instead of the day's lead.
    This returns the first *max_chars* characters of the HTML-stripped Big Picture,
    truncated on a word boundary, with characters that would break YAML front
    matter (double quotes, backslashes, newlines, colons at line start) removed.
    Returns ``""`` when there is no summary, so callers can omit the field.
    """
    if not global_summary_text:
        return ""
    plain = html_module.unescape(_strip_html(global_summary_text))
    plain = re.sub(r'[\[\]\{\}]', ' ', plain)      # no accidental flow structures
    plain = plain.replace('"', "'").replace("\\", " ")
    plain = re.sub(r'\s+', ' ', plain).strip()
    if len(plain) <= max_chars:
        return plain
    cut = plain[:max_chars]
    # Back off to the last complete word so we never split mid-word.
    if ' ' in cut:
        cut = cut.rsplit(' ', 1)[0]
    return cut.rstrip() + '…'

def generate_audio(text: str, output_path: Path, voice: str = "en-US-AriaNeural", rate: str = "+0%") -> bool:
    """Generate an MP3 audio file from text using edge-tts.
    
    Returns True on success, False on failure. Failures are logged but never raise.
    The caller should check the return value and simply skip audio embedding
    when generation fails (degraded mode, not a build-breaker).
    """
    if not text.strip():
        logging.warning(f"TTS: empty text, skipping {output_path}")
        return False
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        result = subprocess.run(
            ["edge-tts", "--voice", voice, "--rate", rate,
             "--text", text, "--write-media", str(output_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logging.error(f"TTS edge-tts failed (rc={result.returncode}): {result.stderr[:200]}")
            return False
        if not output_path.exists() or output_path.stat().st_size < 100:
            logging.error(f"TTS: output file missing or too small: {output_path}")
            return False
        logging.info(f"TTS: generated {output_path} ({output_path.stat().st_size // 1024}KB)")
        return True
    except FileNotFoundError:
        logging.error("TTS: edge-tts not found on PATH; audio generation disabled")
        return False
    except subprocess.TimeoutExpired:
        logging.error(f"TTS: edge-tts timed out for {output_path}")
        return False
    except Exception as e:
        logging.error(f"TTS: unexpected error: {e}")
        return False

def generate_json_ld(
    edition_label: str,
    post_now: datetime,
    global_summary_text: str | None,
    section_titles: list[str],
    sources: list[str],
    site_url: str = "https://cokev-bot.github.io/ai-news/",
) -> str:
    """Generate a JSON-LD NewsArticle schema block for embedding in post HTML.

    Returns a <script type=\"application/ld+json\"> block with structured data
    that search engines can use to index the edition as a news article.

    Args:
        edition_label: Human-readable edition name (e.g. "Morning").
        post_now: The datetime used for the post (Pacific or UTC).
        global_summary_text: The raw text of the Big Picture summary (used as
            the headline/description). May be None if no summary was generated.
        section_titles: List of section titles in this edition.
        sources: Unique source names from this edition's articles.
        site_url: Base URL of the site for constructing the URL field.
    """
    date_published = post_now.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Strip trailing zeros from timezone offset for cleaner ISO 8601
    # e.g. -0700 -> -07:00
    if len(date_published) > 5 and date_published[-5] in ("+", "-"):
        date_published = date_published[:-2] + ":" + date_published[-2:]

    headline = (
        f"AI News Digest — {edition_label} Edition"
    )
    # Use the Big Picture text as the description, truncated to 300 chars
    description = ""
    if global_summary_text:
        description = global_summary_text.strip()
        if len(description) > 300:
            description = description[:297].rsplit(" ", 1)[0] + "…"

    article_section = ", ".join(section_titles) if section_titles else "AI News"
    keywords = list(dict.fromkeys(sources))[:20]  # unique, capped at 20

    post_date = post_now.strftime("%Y-%m-%d")
    post_slug = f"{post_date}-{edition_label.lower()}"
    article_url = f"{site_url.rstrip('/')}/{post_date}-{edition_label.lower()}.html"

    schema = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": headline,
        "datePublished": date_published,
        "articleSection": article_section,
        "keywords": keywords,
        "url": article_url,
        "publisher": {
            "@type": "Organization",
            "name": "AI News Digest",
            "url": site_url.rstrip("/"),
        },
    }
    if description:
        schema["description"] = description

    json_str = json.dumps(schema, ensure_ascii=False, indent=2)
    return f'<script type="application/ld+json">\n{json_str}\n</script>'


def _slugify(text: str) -> str:
    """Convert a section title to a URL-safe slug for filenames."""
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug[:60]


def _subsection_key(section_index: int, subsection_index: int) -> str:
    """Stable grouping key for one subsection: its position in sections.json.

    Subsection *titles* are not unique — "OpenAI", "Google", "Anthropic" and
    "Mistral" each appear under more than one section — so keying the article
    grouping by title made one section's articles render verbatim under every
    other section that reused the title. Measured on the published archive:
    158 of 194 posts carried the same items twice (AI Labs ≡ Developers), and
    the section summaries described an identical article list under two
    headings. Key by (section, subsection) position instead; both the fetch
    path and every render path go through this one function so they cannot
    drift apart.
    """
    return f"{section_index}:{subsection_index}"

def generate_edition_audio(
    edition: str,
    site_root: Path,
    global_summary_text: str | None,
    section_summaries: dict[str, str],
    config: dict | None = None,
) -> dict[str, str]:
    """Generate MP3 audio files for each section and the Big Picture.
    
    Returns a dict mapping section slugs to their audio path relative to site_root
    (e.g. 'assets/audio/2026-06-10-afternoon/big-picture.mp3').
    Only includes entries for successfully generated audio files.
    
    If TTS is disabled in config, returns an empty dict immediately.
    """
    cfg = config or load_config(site_root)
    tts_cfg = get_tts_config(site_root) if config is None else {**DEFAULT_TTS_CONFIG, **cfg.get("tts", {})}
    
    if not tts_cfg.get("enabled", True):
        logging.info("TTS: disabled in config, skipping audio generation")
        return {}
    
    audio_dir = site_root / "assets" / "audio" / edition
    voice = tts_cfg["voice"]
    rate = tts_cfg["rate"]
    audio_paths: dict[str, str] = {}
    
    # Big Picture audio
    if global_summary_text:
        clean_text = _strip_html(global_summary_text)
        if clean_text:
            bp_path = audio_dir / "big-picture.mp3"
            rel_path = f"assets/audio/{edition}/big-picture.mp3"
            if generate_audio(clean_text, bp_path, voice=voice, rate=rate):
                audio_paths["big-picture"] = rel_path
    
    # Section summaries
    for section_title, summary_text in section_summaries.items():
        if not summary_text or summary_text == "Summary could not be generated.":
            continue
        clean_text = _strip_html(summary_text)
        if clean_text:
            slug = _slugify(section_title)
            sec_path = audio_dir / f"{slug}.mp3"
            rel_path = f"assets/audio/{edition}/{slug}.mp3"
            if generate_audio(clean_text, sec_path, voice=voice, rate=rate):
                audio_paths[slug] = rel_path
    
    logging.info(f"TTS: generated {len(audio_paths)} audio file(s) for {edition}")
    return audio_paths

def audio_player_html(audio_path: str, label: str = "Listen") -> str:
    """Return an HTML audio player snippet for a given audio file path.
    
    Uses a minimal, accessible <audio> element with a download link fallback.
    The path should be relative to the site root or absolute.
    """
    # Prepend base path for Jekyll
    full_path = f"/ai-news/{audio_path.lstrip('/')}"
    aria = html_module.escape(label, quote=True)
    return (
        f'<div class="audio-player" style="margin: 8px 0;">'
        f'<audio controls preload="none" style="width:100%;max-width:400px;" '
        f'aria-label="Audio summary of {aria}">'
        f'<source src="{full_path}" type="audio/mpeg">'
        f'<a href="{full_path}">Download {aria}</a>'
        f'</audio></div>'
    )


# ---------------------------------------------------------------------------
# OG Image (Social Card) Generation
# ---------------------------------------------------------------------------

DEFAULT_OG_IMAGE_CONFIG = {
    "enabled": True,
}


def get_og_image_config(site_root: Path) -> dict:
    """Load OG image config from config.json, merged with defaults."""
    cfg = load_config(site_root)
    og = cfg.get("og_image", {})
    return {**DEFAULT_OG_IMAGE_CONFIG, **og}


def generate_og_image_for_edition(
    edition: str,
    site_root: Path,
    global_summary_text: str | None,
) -> str | None:
    """Generate an OG social card image for an edition.

    Delegates to ``tools/make_og_image.py``, which owns the canonical
    implementation (edition parsing, Pillow rendering, path derivation).
    Returns the relative path to the PNG (e.g. "assets/og/2026-06-18-Morning.png")
    on success, or None on failure (graceful degradation — the post is still
    published without an og:image, just without a preview card on social).
    """
    # Import the OG image tool. It's in tools/ which may not be on the
    # default sys.path, so we add it dynamically.
    tools_dir = str((site_root / "tools").resolve())
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    try:
        from make_og_image import generate_og_image_for_edition as _make_og
    except ImportError:
        logging.warning("OG image: make_og_image module not found; OG image generation skipped")
        return None

    try:
        return _make_og(edition, site_root, global_summary_text)
    except Exception as e:
        logging.error(f"OG image generation failed for {edition}: {e}")
        return None


# ---------------------------------------------------------------------------
# Feed health tracking
# ---------------------------------------------------------------------------

HEALTH_FILE = ".feed_health.json"


def _load_feed_health(site_root: Path) -> dict:
    """Load ``.feed_health.json``; empty dict when missing or corrupt."""
    path = site_root / HEALTH_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_feed_health(site_root: Path, health: dict) -> None:
    """Write ``.feed_health.json`` atomically. Never raises."""
    try:
        path = site_root / HEALTH_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(health, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:  # pragma: no cover - disk-level failure only
        logging.warning(f"Could not write feed health state: {e}")


def record_feed_health(site_root: Path, results: list[dict]) -> None:
    """Merge this run's fetch outcomes into ``.feed_health.json``.

    *results* is a list of ``{name, url, ok, error}`` dicts — one per feed
    attempt. Successes stamp ``last_success`` and reset the failure streak;
    failures stamp ``last_failure`` and increment ``consecutive_failures``.

    The edition pipeline is the only thing that actually fetches every feed on
    a schedule, so recording health here (rather than relying solely on the
    standalone ``tools/check_feeds.py`` monitor) is what keeps the public
    source-status page's "last successful fetch" column honest.

    This is deliberately a separate, best-effort step: a failure to persist
    health must never fail an edition.
    """
    if not results:
        return
    health = _load_feed_health(site_root)
    now_iso = datetime.now(timezone.utc).isoformat()
    for res in results:
        name = res.get("name")
        if not name:
            continue
        entry = health.get(name)
        if not isinstance(entry, dict):
            entry = {
                "url": res.get("url", ""),
                "consecutive_failures": 0,
                "last_success": None,
                "last_failure": None,
                "last_error": None,
                "last_post": None,
            }
        entry["url"] = res.get("url", entry.get("url", ""))
        if res.get("ok"):
            entry["consecutive_failures"] = 0
            entry["last_success"] = now_iso
            entry["last_error"] = None
            # Content freshness, distinct from reachability. ``last_success``
            # only proves the HTTP request worked; ``last_post`` is the newest
            # publication date among every item the feed served. A reachable
            # feed whose newest item is weeks old is a coverage problem that
            # fetch status alone cannot express.
            #
            # Only write a freshness reading that was actually observed. A
            # success with no dated item (an undated feed, or an empty-but-
            # valid body) must NOT clear a previously good last_post: doing so
            # would turn "known stale" into "unknown", which is strictly less
            # actionable. last_post == None means "never observed a date",
            # which is different from "the newest item is old".
            if res.get("last_post"):
                entry["last_post"] = res["last_post"]
        else:
            # A failed fetch carries no freshness information — leave
            # last_post untouched so the last known value survives, and the
            # staleness it implies keeps growing while the feed is down.
            entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
            entry["last_failure"] = now_iso
            entry["last_error"] = res.get("error") or "all URLs failed"
        # Every entry carries the key, even the ones that have never observed a
        # date (entries written before freshness tracking existed, or a feed
        # serving only undated items). setdefault only fills a gap: it can
        # never overwrite a recorded value.
        entry.setdefault("last_post", None)
        health[name] = entry
    _save_feed_health(site_root, health)


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

def _looks_like_rss(body: bytes) -> bool:
    """Cheap check that the body is actually an RSS/Atom payload, not an empty
    200 or an HTML error page. nitter.net and friends have been known to
    return 200 OK with an empty body when rate-limited. The 100-byte floor
    is small enough to admit tiny test fixtures and is still well below any
    real feed's size (a feed with one item is usually 1-2 KB)."""
    if not body or len(body) < 100:
        return False
    head = body[:4096].lstrip().lower()
    return (b"<rss" in head) or (b"<feed" in head) or (b"<channel" in head)


_CURL_AVAILABLE: bool | None = None


def _curl_available() -> bool:
    """Whether the ``curl`` binary is on PATH. Cached after the first check.

    26 of 34 feeds depend on this transport, and if curl disappears they do not
    fail loudly — they silently revert to xcancel's 1971 placeholder. Checking
    once and logging a single clear error turns that into an unmissable signal
    instead of ~78 per-feed warnings spread through the run log.
    """
    global _CURL_AVAILABLE
    if _CURL_AVAILABLE is None:
        _CURL_AVAILABLE = shutil.which("curl") is not None
        if not _CURL_AVAILABLE:
            logging.error(
                "curl is not on PATH — the xcancel feed transport is unavailable "
                "and all X/Twitter feeds will return placeholder data. "
                "Install curl (apt-get install curl) to restore them."
            )
    return _CURL_AVAILABLE


def _http_get_with_curl(url: str, *, timeout: int = 20) -> bytes | None:
    """GET *url* using the system ``curl`` binary instead of urllib.

    Needed for the xcancel RSS mirror, which serves a 1971-dated "RSS reader
    not yet whitelisted!" placeholder to Python's TLS/HTTP stack no matter what
    headers are sent, while serving real content to ``curl`` with an identical
    request (verified 2026-09-13: same method, path, host and User-Agent — 1442
    bytes of placeholder via urllib/http.client/aiohttp/curl_cffi, 31949 bytes
    of 20 real items via curl). The discriminator is below the HTTP layer, so it
    cannot be fixed by headers; shelling out to curl is the pragmatic workaround.

    Returns body bytes, or None on any failure. Never raises.
    """
    if not _curl_available():
        return None
    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), "-A", XCANCEL_UA, url],
            capture_output=True,
            timeout=timeout + 10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logging.warning(f"curl transport failed for {url}: {e}")
        return None
    raw = proc.stdout
    if not raw:
        return None
    return raw


def _http_get_with_retry(url: str, *, timeout: int = 15, attempts: int = 3,
                         backoff_base: float = 0.6) -> bytes | None:
    """GET a URL with exponential backoff. Returns the body bytes on success,
    or None if all attempts fail (network error, non-200, or body fails the
    RSS-shape check). Never raises — callers don't need try/except.

    xcancel mirror URLs are routed through ``_http_get_with_curl`` because that
    host refuses real content to Python's HTTP stack (see its docstring).
    """
    last_err = ""
    use_curl = "xcancel.com" in url
    for attempt in range(1, attempts + 1):
        try:
            if use_curl:
                raw = _http_get_with_curl(url, timeout=timeout)
                if raw is not None and _looks_like_rss(raw):
                    return raw
                last_err = (
                    "curl transport returned no usable RSS body"
                    if raw is None else
                    f"empty/non-RSS body ({len(raw)} bytes) via curl"
                )
            else:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "AI-News-Digest/1.1 (+https://cokev-bot.github.io/ai-news/)"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                if _looks_like_rss(raw):
                    return raw
                last_err = f"empty/non-RSS body ({len(raw)} bytes)"
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError) as e:
            last_err = f"{type(e).__name__}: {e}"
        except Exception as e:  # pragma: no cover
            last_err = f"{type(e).__name__}: {e}"
        if attempt < attempts:
            time.sleep(backoff_base * (2 ** (attempt - 1)))
    return None


def fetch_feed(name: str, url: str, fallbacks: list[str] | None = None, *, max_items_per_source: int = MAX_ITEMS_PER_SOURCE, max_age_days: int = MAX_AGE_DAYS, backoff_base: float = 0.6, health_sink: list[dict] | None = None) -> list[dict]:
    """Fetch and parse an RSS feed, returning a list of article dicts.

    `fallbacks` is an ordered list of alternative URLs to try if the primary
    URL fails (network error, HTTP error, or empty/non-RSS body). Each URL
    goes through the same retry/backoff logic. We log one line per attempt so
    the run log makes feed health observable. We never raise — a single bad
    feed cannot abort the whole edition.

    `health_sink`, when given, receives one ``{name, url, ok, error}`` dict
    describing this attempt so the caller can persist feed health. The body
    being fetched successfully is what counts as success here — a feed that
    returns a valid but empty/aged-out body is still "reachable".
    """
    fallbacks = list(fallbacks or [])
    candidates = [url] + fallbacks
    raw: bytes | None = None
    used_idx: int = -1
    for idx, candidate in enumerate(candidates):
        raw = _http_get_with_retry(candidate, timeout=15, attempts=3, backoff_base=backoff_base)
        if raw is not None:
            used_idx = idx
            break
        if idx == 0:
            logging.warning(f"{name}: primary failed, trying {len(fallbacks)} fallback(s)")
    if raw is None:
        logging.error(f"{name}: all {len(candidates)} URL(s) failed — feed skipped")
        if health_sink is not None:
            health_sink.append({
                "name": name,
                "url": url,
                "ok": False,
                "error": f"all {len(candidates)} URL(s) failed",
                # No body was parsed, so this run knows nothing about freshness.
                # None here must not overwrite a previously recorded last_post
                # (record_feed_health only writes last_post on success).
                "last_post": None,
            })
        return []
    if used_idx > 0:
        logging.info(f"{name}: served by fallback #{used_idx} ({candidates[used_idx]})")

    try:
        # lstrip() is required, not cosmetic: the xcancel mirror prefixes its
        # XML declaration with two whitespace bytes, and expat rejects a
        # declaration that is not at byte 0 ("XML or text declaration not at
        # start of entity: line 1, column 2"). Without this the entire X feed
        # set parses to zero items. Verified 2026-09-13.
        root = ET.fromstring(raw.lstrip())
    except Exception as e:
        logging.error(f"Failed to parse {name}: {e}")
        if health_sink is not None:
            health_sink.append({
                "name": name,
                "url": url,
                "ok": False,
                "error": f"parse error: {e}",
                "last_post": None,
            })
        return []

    if health_sink is not None:
        health_sink.append({
            "name": name,
            "url": url,
            "ok": True,
            "error": None,
            # Filled in below, once every item has been parsed. Set here so the
            # key is always present in the sink entry's shape.
            "last_post": None,
        })

    articles = []
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (max_age_days * 86400)

    # Newest publication date seen among EVERY parsed item, tracked before the
    # age filter and before the retweet filter. Both of those filters discard
    # items, so computing this afterwards would report None for exactly the
    # feeds we need to catch: a source that is reachable but has not published
    # in weeks (or an xcancel whitelist placeholder, which is a single
    # 1971-dated item) would otherwise look like a healthy, merely quiet feed.
    # This is the content-freshness half of feed health; ``last_success``
    # alone only proves the HTTP request worked.
    last_post: datetime | None = None

    for item in root.findall(".//item")[:max_items_per_source]:
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
        if pub_dt is not None and (last_post is None or pub_dt > last_post):
            last_post = pub_dt
        if pub_dt is not None and pub_dt.timestamp() < cutoff:
            continue

        # Filter out retweet-only items (low-signal Nitter noise)
        if is_retweet(title):
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

    # Publish the freshness signal on the same entry the fetch already recorded,
    # so the health file carries status AND content freshness without changing
    # this function's return contract (callers still get a plain article list).
    if health_sink is not None:
        for entry in reversed(health_sink):
            if entry.get("name") == name and entry.get("url") == url:
                entry["last_post"] = last_post.isoformat() if last_post else None
                break

    return articles


def fetch_all_feeds(sections: list[dict], *, max_items_per_source: int = MAX_ITEMS_PER_SOURCE, max_age_days: int = MAX_AGE_DAYS, health_sink: list[dict] | None = None) -> dict[str, list[tuple[str, list[dict]]]]:
    """Fetch all RSS feeds in parallel, grouped by subsection.

    Returns a mapping of subsection_title → ordered list of
    (feed_name, articles) tuples — one per feed — in the same order
    the feeds appear in sections.json.  Errors in individual feeds are
    logged but never propagated (fetch_feed never raises).

    `health_sink`, when given, collects one ``{name, url, ok, error}`` dict
    per feed attempt (see ``fetch_feed``) so the caller can persist feed
    health. It is appended to from worker threads, which is safe for
    ``list.append`` under the GIL.
    """
    # Collect every feed with its subsection context.
    feed_jobs: list[tuple[str, str, list[str], str]] = []  # (name, url, fallbacks, sub_key)
    subsection_order: list[str] = []
    seen_subkeys: set[str] = set()
    for section_index, section in enumerate(sections):
        for subsection_index, subsection in enumerate(section["subsections"]):
            sub_key = _subsection_key(section_index, subsection_index)
            alts_map = subsection.get("feeds_alts", {}) or {}
            for feed_name, feed_url in subsection["feeds"].items():
                feed_fallbacks = alts_map.get(feed_name, []) or []
                feed_jobs.append((feed_name, feed_url, feed_fallbacks, sub_key))
            if sub_key not in seen_subkeys:
                subsection_order.append(sub_key)
                seen_subkeys.add(sub_key)

    # Parallel fetch — each worker calls fetch_feed which never raises.
    raw_results: dict[tuple[str, str, str, str], list[dict]] = {}
    num_workers = min(MAX_FEED_WORKERS, len(feed_jobs)) if feed_jobs else 1
    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        future_to_job = {
            pool.submit(fetch_feed, name, url, fallbacks=fallbacks, max_items_per_source=max_items_per_source, max_age_days=max_age_days, health_sink=health_sink): (name, url, fallbacks, sub_key)
            for name, url, fallbacks, sub_key in feed_jobs
        }
        for future in as_completed(future_to_job):
            name, url, fallbacks, sub_key = future_to_job[future]
            try:
                articles = future.result()
            except Exception:
                # fetch_feed shouldn't raise, but guard anyway.
                logging.exception(f"{name}: unexpected error in parallel fetch")
                articles = []
            raw_results[(name, url, "|".join(fallbacks), sub_key)] = articles
            print(f"  → {name}… {len(articles)} fetched (parallel)")

    # Reassemble in canonical section/subsection/feed order so dedup is
    # deterministic.
    results: dict[str, list[tuple[str, list[dict]]]] = {k: [] for k in subsection_order}
    for name, url, fallbacks, sub_key in feed_jobs:
        key = (name, url, "|".join(fallbacks), sub_key)
        results[sub_key].append((name, raw_results.get(key, [])))

    return results


# ---------------------------------------------------------------------------
# Big Picture daily cache
# ---------------------------------------------------------------------------
#
# The "The Big Picture" global summary normally takes a slow LLM call to
# generate. Morning/Afternoon/Evening all run the same day in Pacific time
# and observe largely the same news; regenerating the Big Picture 3x per
# day is wasteful and produces drift (different wording for the same day
# in different editions). We cache it once per PT-day in
# ``<pt-date>-bp.json`` and reuse the cached summary text + rendered HTML
# across same-day editions.
#
# The cache key is the PT date (e.g. ``2026-06-05``), not the edition name,
# so Morning/Afternoon/Evening all share. A small article fingerprint is
# stored alongside the summary so we can detect a substantively different
# article set and regenerate (e.g. if the cache file from a previous
# day is somehow read against a wildly different article set).
# ---------------------------------------------------------------------------


def _big_picture_fingerprint(articles: list[dict]) -> str:
    """Stable short hash of an article set, independent of order.

    Two article sets that share the same (source, title) pairs hash equal,
    so reordering or shifting the same set across editions produces the
    same fingerprint. We do not include the link — multiple X statuses can
    share titles — but (source, title) is a good "the news is the same"
    proxy for the same-day reuse case.
    """
    import hashlib
    pairs = sorted(
        ((a.get("source", ""), a.get("title", "")) for a in articles),
        key=lambda p: (p[0].lower(), p[1].lower()),
    )
    payload = "\u241f".join(
        f"{s.lower()}\u241e{t.lower()}" for s, t in pairs
    ).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def _big_picture_cache_path(site_root: Path, pt_date_str: str) -> Path:
    """Path to the per-day Big Picture cache file.

    Filename: ``<pt-date>-bp.json`` (e.g. ``2026-06-05-bp.json``), per the
    ROADMAP Phase 4 spec. The file is gitignored (see ``*-bp.json`` rule
    in ``.gitignore``) since it is a runtime artifact that is overwritten
    multiple times per day.
    """
    return site_root / f"{pt_date_str}-bp.json"


def load_big_picture_cache(site_root: Path, pt_date_str: str) -> dict | None:
    """Load a per-day Big Picture cache if it exists and is well-formed.

    Returns the cache dict (``{date, generated_at, fingerprint,
    summary_text, summary_html}``) on success, or ``None`` if the file is
    missing, unreadable, or missing required fields. The caller is
    responsible for fingerprint comparison; this function does not
    invalidate the cache on its own.
    """
    path = _big_picture_cache_path(site_root, pt_date_str)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"Big Picture cache at {path} unreadable: {e}")
        return None
    required = ("date", "fingerprint", "summary_text", "summary_html")
    if not all(k in data for k in required):
        logging.warning(
            f"Big Picture cache at {path} missing required fields; ignoring."
        )
        return None
    return data


def save_big_picture_cache(
    site_root: Path,
    pt_date_str: str,
    fingerprint: str,
    summary_text: str,
    summary_html: str,
) -> None:
    """Persist a per-day Big Picture cache. Best-effort: failure is logged
    but does not abort edition generation (the post has already been
    rendered; we just won't get a same-day reuse next time)."""
    path = _big_picture_cache_path(site_root, pt_date_str)
    payload = {
        "date": pt_date_str,
        "generated_at": pacific_now().isoformat(),
        "fingerprint": fingerprint,
        "summary_text": summary_text,
        "summary_html": summary_html,
    }
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logging.warning(f"Failed to write Big Picture cache to {path}: {e}")


# ---------------------------------------------------------------------------
# Static JSON API (machine-readable edition payloads)
# ---------------------------------------------------------------------------

# Payloads are written to a real on-disk directory which the Jekyll build
# copies verbatim, so each edition is served at /api/<YYYY-MM-DD>-<Edition>.json
# (e.g. /ai-news/api/2026-09-13-Evening.json). They live inside the site root
# because that is what the build mirrors into _site/.
#
# They are NOT written next to the post in _posts/: Jekyll treats every file in
# a collection directory as a document and silently drops it when it is neither
# a post (.html/.md, carrying front matter) nor a static-file type it copies —
# verified by probe, a JSON file in _posts/ never reaches _site/.
API_DIR = "api"
DEFAULT_SITE_URL = "https://cokev-bot.github.io/ai-news/"
# Mirror _make_description()'s budget so the JSON and the post's front-matter
# `description:` cannot drift apart.
MAX_DESCRIPTION_CHARS = 160


def _iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 UTC string for a datetime, or None when there is no timestamp.

    A naive datetime is treated as UTC (feed pub_dt values are built from
    parsed RFC-822 dates and are normally aware, but a naive one must not
    silently render as a local-time string).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _article_api_entry(art: dict) -> dict:
    """One article as a JSON-serialisable dict (sorted keys, no HTML)."""
    return {
        "title": art.get("title") or art.get("link", ""),
        "link": _article_link(art),
        "source": art.get("source", ""),
        "description": art.get("description", ""),
        "published": _iso_utc(art.get("pub_dt")),
    }


def _edition_title(edition_label: str | None) -> str:
    """The edition's human title, matching the post's own front-matter title."""
    if not edition_label:
        return "AI News Digest"
    return f"AI News Digest — {edition_label} Edition"


def _feeds_scanned(header_fragments: list[str] | None) -> int:
    """Pull the feed count back out of the rendered ``Scanning N feeds`` line.

    The fetch pass owns that number (it counts every feed in sections.json,
    including ones that failed), so reusing it keeps the JSON and the post in
    agreement by construction rather than by two parallel computations that
    could drift.
    """
    for fragment in header_fragments or []:
        m = re.match(r"Scanning (\d+) feeds", fragment.strip())
        if m:
            return int(m.group(1))
    return 0


def build_edition_api_payload(
    *,
    edition_label: str | None,
    post_now: datetime,
    generated_at: datetime,
    global_summary_text: str | None,
    section_summaries: dict[str, str] | None,
    sections_data: list[dict],
    subsection_articles: dict[str, list[dict]],
    freshness: dict[str, int] | None = None,
    reading_minutes: int = 0,
    header_fragments: list[str] | None = None,
    site_url: str = DEFAULT_SITE_URL,
    description_chars: int = MAX_DESCRIPTION_CHARS,
) -> dict:
    """Build the machine-readable payload for one edition.

    Pure: takes the already-computed render inputs and returns a
    JSON-serialisable dict. No LLM call, no network, no writes — so the
    endpoint is cheap enough to emit on every run (including republish).

    The section/subsection walk deliberately mirrors the post render exactly:
    articles are looked up by the subsection's *positional* key (the key
    ``subsection_articles`` actually has) and the post emits one ``<h3>`` per
    populated subsection of a section. Subsection titles are NOT unique in
    sections.json — "OpenAI", "Google", "Anthropic" and "Mistral" each appear
    under more than one section — so keying by title would make one section's
    articles appear under every section that reuses the title. Positional keys
    keep each feed owned by exactly one section, in both the post and this
    payload, so the payload's item count and per-section membership agree with
    the HTML a reader sees.
    """
    base = site_url.rstrip("/")
    day_path = post_now.strftime("%Y/%m/%d")
    post_url = (
        f"{base}/news/{day_path}/{edition_label}/" if edition_label else base + "/"
    )

    sections_out: list[dict] = []
    sources: list[str] = []

    for section_index, section in enumerate(sections_data):
        s_title = section.get("title", "")
        section_articles: list[dict] = []
        subsections_out: list[dict] = []

        for subsection_index, subsection in enumerate(section.get("subsections", [])):
            ss_title = subsection.get("title", "")
            sub_key = _subsection_key(section_index, subsection_index)
            arts = subsection_articles.get(sub_key) or []
            if not arts:
                continue
            section_articles.extend(arts)
            subsections_out.append({
                "title": ss_title,
                "articles": [_article_api_entry(a) for a in arts],
            })

        if not section_articles:
            # Empty sections are omitted entirely, matching the post render.
            continue

        summary_html = (section_summaries or {}).get(s_title, "") or ""
        sections_out.append({
            "title": s_title,
            # Anchor matches format_section_heading()'s <h2> id.
            "url": f"{post_url}#{_slugify(s_title)}",
            "item_count": len(section_articles),
            "summary": _strip_html(summary_html),
            "subsections": subsections_out,
        })

        for art in section_articles:
            src = art.get("source")
            if src and src not in sources:
                sources.append(src)

    freshness = freshness or {}
    total_words = sum(count_words(s["summary"]) for s in sections_out)
    plain_bp = _strip_html(global_summary_text) if global_summary_text else ""

    return {
        "edition": edition_label,
        "date": post_now.strftime("%Y-%m-%d"),
        "published": post_now.isoformat(),
        "generated_at": generated_at.isoformat(),
        "title": _edition_title(edition_label),
        "url": post_url,
        "summary": plain_bp[:description_chars],
        "big_picture": plain_bp,
        "reading_time_minutes": int(reading_minutes or 0),
        "stats": {
            "feeds_scanned": _feeds_scanned(header_fragments),
            "sources": len(sources),
            "items": sum(s["item_count"] for s in sections_out),
            "fresh": int(freshness.get("fresh", 0)),
            "stale": int(freshness.get("stale", 0)),
            "from_yesterday": int(freshness.get("yesterday", 0)),
            "words": total_words,
        },
        "sources": sorted(sources, key=str.lower),
        "sections": sections_out,
    }


def write_edition_api(site_root: Path, edition: str, payload: dict) -> Path | None:
    """Write ``api/<edition>.json`` atomically. Never raises.

    Best-effort by design: the edition post is already on disk when this runs,
    so a read-only or full filesystem must not cost the edition. Returns the
    written path, or None when the write failed.
    """
    if not edition:
        return None
    api_dir = site_root / API_DIR
    path = api_dir / f"{edition}.json"
    try:
        api_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as e:
        logging.warning(f"Edition API payload write failed for {path}: {e}")
        return None
    return path


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
    tuning = get_tuning(site_root)

    # LOAD SECTIONS FROM EXTERNAL JSON
    sections_path = site_root / "sections.json"
    if not sections_path.exists():
        print(f"  [!] Error: {sections_path} not found.")
        return False
    try:
        _sections_data = json.loads(sections_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [!] Error parsing sections.json: {e}")
        return False

    # Backward-compatible: sections.json may be a flat list (old) or an
    # object with "sections" + "source_urls" keys (new).
    if isinstance(_sections_data, list):
        SECTIONS = _sections_data
        SOURCE_URLS: dict[str, str] = {}
    else:
        SECTIONS = _sections_data.get("sections", [])
        SOURCE_URLS = _sections_data.get("source_urls", {})

    state_path = site_root / ".news_state.json"
    state = load_state(state_path)
    seen_links: dict[str, dict] = state.get("seen_links", {})

    subsection_articles: dict[str, list[dict]] = {}

    if republish:
        # Reconstruct articles directly from state — no feed fetching needed
        for section_index, section in enumerate(SECTIONS):
            for subsection_index, subsection in enumerate(section["subsections"]):
                subsection_articles[_subsection_key(section_index, subsection_index)] = []

        # Build a reverse map: feed_name → feed_url for all feeds, used to
        # recover the feed name from a nitter username extracted from a link.
        feed_url_by_name: dict[str, str] = {}
        for section in SECTIONS:
            for subsection in section["subsections"]:
                for feed_name, feed_url in subsection["feeds"].items():
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
            # Find which subsection this feed belongs to. Positional keys, and
            # first-match-wins: a feed listed under more than one subsection is
            # assigned to the first, never duplicated into both.
            sub_key = None
            for section_index, section in enumerate(SECTIONS):
                for subsection_index, subsection in enumerate(section["subsections"]):
                    if feed_name in subsection["feeds"]:
                        sub_key = _subsection_key(section_index, subsection_index)
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
        # Fresh run: fetch feeds in parallel, then deduplicate in order
        seen_this_run: list[dict] = []

        # Initialise every subsection key so empty subsections still appear
        for section_index, section in enumerate(SECTIONS):
            for subsection_index, subsection in enumerate(section["subsections"]):
                subsection_articles[_subsection_key(section_index, subsection_index)] = []

        # Collect per-feed fetch outcomes so the public source-status page can
        # report "last successful fetch" from the same runs that produce
        # editions. Recorded before dedup so a fully-deduplicated edition
        # (which returns False below) still updates feed health.
        health_sink: list[dict] = []
        feed_results = fetch_all_feeds(SECTIONS, max_items_per_source=tuning["max_items_per_source"], max_age_days=tuning["max_age_days"], health_sink=health_sink)
        try:
            record_feed_health(site_root, health_sink)
            ok_count = sum(1 for r in health_sink if r.get("ok"))
            logging.info(f"Feed health recorded: {ok_count}/{len(health_sink)} fetched OK")
        except Exception as e:  # pragma: no cover - health must never break an edition
            logging.warning(f"Feed health recording failed: {e}")

        for section_index, section in enumerate(SECTIONS):
            for subsection_index, subsection in enumerate(section["subsections"]):
                sub_key = _subsection_key(section_index, subsection_index)
                for feed_name, articles in feed_results.get(sub_key, []):
                    for a in articles:
                        if not is_duplicate(a, seen_this_run, seen_links,
                                            title_sim_threshold=tuning["title_sim_threshold"],
                                            cross_edition_dedup_hours=tuning["cross_edition_dedup_hours"]):
                            seen_this_run.append(a)
                            subsection_articles[sub_key].append(a)
                            print(f"      + kept: {a['title'][:60]}")
                        else:
                            print(f"      - dup:  {a['title'][:60]}")

        if not seen_this_run:
            # Distinguish a genuinely quiet edition from a degraded one. Both
            # produce zero publishable items, but only one is a fault, and
            # conflating them is dangerous in both directions:
            #
            #   * Returning False for a quiet edition aborts run_edition.sh
            #     (set -e) before the Jekyll build and commit, so a routine
            #     all-deduplicated slot looks like a failure and produces no
            #     post — a silent gap on the site.
            #   * Returning True for an outage would hide the far more serious
            #     failure, which is exactly the "26 feeds dead, looks healthy"
            #     class of bug.
            #
            # The discriminator is fetch health, not item count: if at least
            # one feed fetched successfully then the sources are reachable and
            # the window is simply exhausted. If zero fetched OK, the sources
            # are broken and this is a real failure. Treating "cannot prove any
            # feed is healthy" as a failure keeps the alerting biased toward
            # over-reporting outages rather than hiding them.
            healthy = sum(1 for r in health_sink if r.get("ok"))
            total = len(health_sink)
            if healthy == 0:
                print(
                    f"  ✗ No articles to publish and 0/{total} feeds fetched "
                    f"successfully — degraded run, not a quiet day."
                )
                return False
            print(
                f"  ✓ No new articles to publish ({healthy}/{total} feeds fetched "
                f"OK; every item is already in a previous edition). Skipping edition."
            )
            return True

        # Persist new links with metadata
        new_entries = {}
        seen_at_now = datetime.now(timezone.utc).isoformat()
        for a in seen_this_run:
            new_entries[a["link"]] = {
                "edition": edition,
                "feed": a["source"],
                "title": a["title"],
                "description": a.get("description", ""),
                "seen_at": seen_at_now,
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

    # Use the configured timezone for all display timestamps by default (cron-driven
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
        post_now = datetime.now(ZoneInfo(get_timezone(site_root)))

    # Derive human-readable edition label from full name (e.g. "Evening" from "2026-04-14-evening")
    edition_label = edition.split("-")[-1].capitalize()

    # A republish must REPRODUCE a post, not re-derive it as if it were new.
    # The frontmatter date drives the Jekyll permalink (/news/YYYY/MM/DD/<ed>/),
    # so stamping the current time moved an already-published post's URL and
    # 404'd every existing link to it. Reuse the original timestamp from the
    # existing post file when present.
    if republish:
        original_dt = _read_post_frontmatter_date(filepath)
        if original_dt is not None:
            # Convert into the site timezone so the rendered header shows the
            # real abbreviation ("PDT"), not a fixed-offset name like
            # "UTC-07:00". Same instant and same wall-clock time, so the
            # frontmatter date (and therefore the permalink) is unchanged.
            post_now = original_dt.astimezone(ZoneInfo(get_timezone(site_root)))
            logging.info(
                f"Republish: preserving original post date {post_now.isoformat()} "
                f"(permalink /news/{post_now.strftime('%Y/%m/%d')}/{edition_label}/)"
            )

    header_dt = post_now.strftime("%Y-%m-%d %H:%M %Z")

    # Timestamp stamped into the static JSON API payload. On a republish this
    # is the ORIGINAL edition time preserved above (not "now"), so re-emitting a
    # payload can never make an already-published edition look freshly rewritten.
    generated_at = post_now

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

    # Compute freshness tally across all items
    all_items = []
    for items in subsection_articles.values():
        all_items.extend(items)
    freshness = compute_freshness_tally(all_items, tuning["max_age_days"], now=post_now, tz_name=get_timezone(site_root))

    html_lines = [
        "---",
        "layout: post",
        f'title: "AI News Digest — {edition_label} Edition"',
        f'date: {post_now.strftime("%Y-%m-%d %H:%M:%S %z")}',
        "categories: news digest",
        "---",
        "",
        "<h2>🤖 AI News — {} Edition · {}</h2>".format(edition_label, header_dt),
    ]

    # The "Scanning ..." header line and the inline <style> block are appended
    # further down, after section summaries (and therefore the reading-time
    # estimate) exist. Order in the rendered post is unchanged:
    # h2 → scanning line → hr → style → Big Picture → sections.

    # Generate global executive summary across ALL sections
    all_articles = []
    for section_index, section in enumerate(SECTIONS):
        for subsection_index, subsection in enumerate(section["subsections"]):
            sub_key = _subsection_key(section_index, subsection_index)
            all_articles.extend(subsection_articles.get(sub_key, []))

    global_summary_text = ""
    global_summary_html = ""
    if all_articles:
        pt_date_str = post_now.strftime("%Y-%m-%d")
        article_fingerprint = _big_picture_fingerprint(all_articles)

        cached = load_big_picture_cache(site_root, pt_date_str)
        if cached and cached.get("fingerprint") == article_fingerprint:
            logging.info(
                f"Reusing 'The Big Picture' from cache for {pt_date_str} "
                f"(fingerprint {article_fingerprint} matches)."
            )
            global_summary_text = cached["summary_text"]
            # Re-render from the cached *text* rather than reusing the cached
            # HTML. The HTML was rendered by whatever version of
            # linkify_summary() was current when it was cached, so reusing it
            # verbatim pins a stale renderer for the whole PT day and no
            # citation-parsing fix can ever reach a cached edition. Rendering is
            # cheap and deterministic (no LLM call), so always do it here.
            global_summary_html = linkify_summary(global_summary_text, all_articles)
        else:
            if cached and cached.get("fingerprint") != article_fingerprint:
                logging.info(
                    f"Big Picture cache fingerprint drift for {pt_date_str} "
                    f"(cached={cached.get('fingerprint')} vs "
                    f"new={article_fingerprint}); regenerating."
                )
            else:
                logging.info(
                    f"Generating global 'The Big Picture' summary for {pt_date_str}..."
                )

            global_prompt_base = "Write a high-level 'The Big Picture' executive summary for this edition. Synthesize the most critical trends and developments across all categories into 1-2 punchy paragraphs. Use the same strict citation format (Source: ID)."

            global_summary_text = get_global_summary(global_prompt_base, all_articles, config)
            global_summary_html = linkify_summary(global_summary_text, all_articles)

            # Persist for the remaining same-day editions. Best-effort: a
            # write failure here does not break this edition's render.
            save_big_picture_cache(
                site_root,
                pt_date_str,
                article_fingerprint,
                global_summary_text,
                global_summary_html,
            )

        # Defer Big Picture HTML rendering to after TTS audio is generated
        # (audio_paths is computed after section_summaries below).

    # Build the (section_title, section_articles) job list once, in the
    # canonical SECTIONS order. We dispatch all summaries concurrently
    # below, then iterate SECTIONS again to render — that way output
    # order is preserved even though the LLM calls run in parallel.
    section_jobs: list[tuple[str, list[dict]]] = []
    section_articles_by_title: dict[str, list[dict]] = {}
    for section_index, section in enumerate(SECTIONS):
        section_articles: list[dict] = []
        for subsection_index, subsection in enumerate(section["subsections"]):
            sub_key = _subsection_key(section_index, subsection_index)
            section_articles.extend(subsection_articles.get(sub_key, []))
        if not section_articles:
            # Skip empty sections entirely — no summary, no HTML block.
            continue
        section_articles_by_title[section["title"]] = section_articles
        section_jobs.append((section["title"], section_articles))

    section_summaries = summarize_sections_concurrent(
        section_jobs, site_root, config
    )

    # Now that section summaries exist we can estimate reading time, so the
    # header line and inline style block are appended here (the h2 and the
    # front matter were already emitted above).
    reading_minutes = compute_reading_time_minutes(
        section_summaries=section_summaries,
        global_summary_text=global_summary_text if all_articles else None,
        subsection_articles=subsection_articles,
    )
    header_fragments = [
        "Scanning {} feeds".format(total_feeds),
        "{} accounts posted".format(num_sources),
        "{} items".format(sum(len(v) for v in subsection_articles.values())),
        format_freshness_tally(freshness),
    ]
    if reading_minutes > 0:
        header_fragments.append(format_reading_time(reading_minutes))

    # Emit the machine-readable edition payload (served at
    # /api/<YYYY-MM-DD>-<Edition>.json). Best-effort: the post is what readers
    # see, so a failed payload write must never cost an edition.
    api_payload = build_edition_api_payload(
        edition_label=edition_label,
        post_now=post_now,
        generated_at=generated_at,
        global_summary_text=global_summary_text if all_articles else None,
        section_summaries=section_summaries,
        sections_data=SECTIONS,
        subsection_articles=subsection_articles,
        freshness=freshness,
        reading_minutes=reading_minutes,
        header_fragments=header_fragments,
    )
    if write_edition_api(site_root, edition, api_payload):
        logging.info(f"Edition API payload written → {API_DIR}/{edition}.json")

    html_lines.append("<p>{}</p>".format(" · ".join(header_fragments)))
    html_lines.append("<hr>")
    html_lines.append("<style>")
    html_lines.extend([
        ".source-pill {",
        "  display: inline-block;",
        "  background: #e8edf2;",
        "  color: #2c3e50;",
        "  font-size: 0.85em;",
        "  font-weight: 600;",
        "  padding: 1px 6px;",
        "  border-radius: 3px;",
        "  text-decoration: none;",
        "  white-space: nowrap;",
        "  vertical-align: baseline;",
        "  margin-right: 2px;",
        "}",
        ".source-pill:hover {",
        "  background: #cdd5de;",
        "}",
        ".section-count {",
        "  color: #7f8c8d;",
        "  font-size: 0.7em;",
        "  font-weight: 500;",
        "}",
    ])
    html_lines.append("</style>")

    # Generate TTS audio files for each section and Big Picture
    audio_paths = generate_edition_audio(
        edition, site_root,
        global_summary_text if all_articles else None,
        section_summaries, config,
    )

    # Generate OG social card image
    og_image_rel_path = None
    og_cfg = get_og_image_config(site_root)
    if og_cfg.get("enabled", True):
        og_image_rel_path = generate_og_image_for_edition(
            edition, site_root,
            global_summary_text if all_articles else None,
        )

    # Inject og:image into front matter if we generated one.
    #
    # The value must NOT carry the site baseurl. jekyll-seo-tag builds og:image
    # as site.url + site.baseurl + page.image, so a "/ai-news/assets/og/x.png"
    # value published as "https://.../ai-news/ai-news/assets/og/x.png" — a 404
    # on every edition. Verified against the live site 2026-09-13.
    if og_image_rel_path:
        # Insert "image:" line before the closing "---" of the front matter
        for idx, line in enumerate(html_lines):
            if idx > 0 and line.strip() == "---":
                # Insert before the closing ---
                html_lines.insert(idx, f"image: /{og_image_rel_path}")
                break

    # Inject an edition-specific `description:` into the front matter so
    # jekyll-seo-tag emits a meaningful og:description / meta description
    # instead of the generic site tagline. Inserted before the same closing
    # "---" as the image line; the field is omitted when there is no summary.
    description = _make_description(global_summary_text if all_articles else None)
    if description:
        for idx, line in enumerate(html_lines):
            if idx > 0 and line.strip() == "---":
                html_lines.insert(idx, f'description: "{description}"')
                break

    # Render Big Picture HTML (deferred from above so audio_paths is available)
    if all_articles and global_summary_html:
        html_lines.append('<div style="background: #f9f9f9; padding: 15px; border-left: 5px solid #ccc; margin-bottom: 20px;">')
        html_lines.append('  <h3 style="margin-top:0;">🌍 The Big Picture</h3>')
        if "big-picture" in audio_paths:
            html_lines.append(f'  {audio_player_html(audio_paths["big-picture"], "Big Picture summary")}')
        html_lines.append(f'  <p>{global_summary_html}</p>')
        html_lines.append('</div>')
        html_lines.append("")

    for section_index, section in enumerate(SECTIONS):
        section_articles = section_articles_by_title.get(section["title"])
        if not section_articles:
            continue

        summary_text = section_summaries.get(
            section["title"],
            "Summary could not be generated.",
        )

        # 3. Linkify the summary text
        summary_html = linkify_summary(summary_text, section_articles)

        # 4. Add section heading (with item-count badge) and summary to HTML
        html_lines.append(format_section_heading(section["title"], len(section_articles)))
        section_slug = _slugify(section["title"])
        if section_slug in audio_paths:
            html_lines.append(audio_player_html(audio_paths[section_slug], f"{section['title']} summary"))
        html_lines.append('<p><strong>Summary:</strong> {}</p>'.format(summary_html))
        html_lines.append("")

        # 5. Build collapsible subsections
        subsections_html_lines = []
        for subsection_index, subsection in enumerate(section["subsections"]):
            sub_key = _subsection_key(section_index, subsection_index)
            items = subsection_articles.get(sub_key, [])
            if not items:
                continue

            subsections_html_lines.append("")
            subsections_html_lines.append("<h3>{}</h3>".format(subsection["title"]))
            subsections_html_lines.append("")
            for art in items:
                subsections_html_lines.append(render_item(art, SOURCE_URLS))
                subsections_html_lines.append("")

        if subsections_html_lines:
            html_lines.append('<details>')
            html_lines.append('  <summary><strong>Subsections</strong></summary>')
            html_lines.extend(subsections_html_lines)
            html_lines.append('</details>')
            html_lines.append("")

    # Inject JSON-LD structured data after front matter (after the second "---")
    section_titles = [
        s["title"] for s in SECTIONS
        if section_articles_by_title.get(s["title"])
    ]
    sources = list(dict.fromkeys(
        a["source"]
        for items in subsection_articles.values()
        for a in items
    ))
    json_ld = generate_json_ld(
        edition_label=edition_label,
        post_now=post_now,
        global_summary_text=global_summary_text if all_articles else None,
        section_titles=section_titles,
        sources=sources,
    )
    # Find the closing front-matter "---" and insert after it
    fm_end = None
    for idx, line in enumerate(html_lines):
        if idx > 0 and line.strip() == "---":
            fm_end = idx
            break
    if fm_end is not None:
        html_lines.insert(fm_end + 1, "")
        html_lines.insert(fm_end + 2, json_ld)
    else:
        # Fallback: prepend
        html_lines.insert(0, json_ld)

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

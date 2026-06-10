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
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

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
OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"

DEFAULT_CONFIG = {
    "model": "gemma4:31b-cloud",
    "summary_prompt_file": "summary_prompt.txt",
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
    new_lower = new_title.lower()

    for existing in seen:
        title_sim = text_similarity(new_title, existing["title"])
        desc_sim  = text_similarity(new_desc, existing.get("description", ""))
        if title_sim >= title_sim_threshold or desc_sim >= title_sim_threshold:
            return True

        model_pattern = re.compile(
            r"(claude[-\s]\d[.\d]*|gpt[-\s]\d[.\d]*|\d+\.\d+[a-z]?)", re.I
        )
        new_models = set(model_pattern.findall(new_lower))
        old_models = set(model_pattern.findall(existing["title"].lower()))
        if new_models and old_models and new_models == old_models:
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
        model_pattern = re.compile(
            r"(claude[-\s]\d[.\d]*|gpt[-\s]\d[.\d]*|\d+\.\d+[a-z]?)", re.I
        )
        new_models = set(model_pattern.findall(new_lower))
        old_models = set(model_pattern.findall(old_title.lower()))
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

def is_retweet(title: str) -> bool:
    """Return True if the title is a retweet-only item from Nitter.

    Nitter renders retweets with titles like 'R to @user: ...'.
    These are low-signal noise that clutter the digest, so we drop them.
    """
    return title.startswith("R to @")


def render_item(art: dict) -> str:
    """Render a single article as HTML.

    Nitter / Twitter feeds:
        <strong>FeedName</strong>: tweet text <a href="x.com/...">🔗</a>

    News / FT feeds:
        <strong>FeedName</strong>: <a href="...">Title</a>
        Summary text
    """
    # Safety net: skip retweet-only items that slipped through fetch_feed
    if is_retweet(art.get("title", "")):
        return ""

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

    response = _query_ollama(full_prompt, model)
    return response or "Summary could not be generated."


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

def _slugify(text: str) -> str:
    """Convert a section title to a URL-safe slug for filenames."""
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug[:60]

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
    return (
        f'<div class="audio-player" style="margin: 8px 0;">'
        f'<audio controls preload="none" style="width:100%;max-width:400px;">'
        f'<source src="{full_path}" type="audio/mpeg">'
        f'<a href="{full_path}">Download {label}</a>'
        f'</audio></div>'
    )


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


def _http_get_with_retry(url: str, *, timeout: int = 15, attempts: int = 3,
                         backoff_base: float = 0.6) -> bytes | None:
    """GET a URL with exponential backoff. Returns the body bytes on success,
    or None if all attempts fail (network error, non-200, or body fails the
    RSS-shape check). Never raises — callers don't need try/except."""
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
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


def fetch_feed(name: str, url: str, fallbacks: list[str] | None = None, *, max_items_per_source: int = MAX_ITEMS_PER_SOURCE, max_age_days: int = MAX_AGE_DAYS) -> list[dict]:
    """Fetch and parse an RSS feed, returning a list of article dicts.

    `fallbacks` is an ordered list of alternative URLs to try if the primary
    URL fails (network error, HTTP error, or empty/non-RSS body). Each URL
    goes through the same retry/backoff logic. We log one line per attempt so
    the run log makes feed health observable. We never raise — a single bad
    feed cannot abort the whole edition.
    """
    fallbacks = list(fallbacks or [])
    candidates = [url] + fallbacks
    raw: bytes | None = None
    used_idx: int = -1
    for idx, candidate in enumerate(candidates):
        raw = _http_get_with_retry(candidate, timeout=15, attempts=3)
        if raw is not None:
            used_idx = idx
            break
        if idx == 0:
            logging.warning(f"{name}: primary failed, trying {len(fallbacks)} fallback(s)")
    if raw is None:
        logging.error(f"{name}: all {len(candidates)} URL(s) failed — feed skipped")
        return []
    if used_idx > 0:
        logging.info(f"{name}: served by fallback #{used_idx} ({candidates[used_idx]})")

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logging.error(f"Failed to parse {name}: {e}")
        return []

    articles = []
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (max_age_days * 86400)

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

    return articles


def fetch_all_feeds(sections: list[dict], *, max_items_per_source: int = MAX_ITEMS_PER_SOURCE, max_age_days: int = MAX_AGE_DAYS) -> dict[str, list[tuple[str, list[dict]]]]:
    """Fetch all RSS feeds in parallel, grouped by subsection.

    Returns a mapping of subsection_title → ordered list of
    (feed_name, articles) tuples — one per feed — in the same order
    the feeds appear in sections.json.  Errors in individual feeds are
    logged but never propagated (fetch_feed never raises).
    """
    # Collect every feed with its subsection context.
    feed_jobs: list[tuple[str, str, list[str], str]] = []  # (name, url, fallbacks, sub_key)
    subsection_order: list[str] = []
    seen_subkeys: set[str] = set()
    for section in sections:
        for subsection in section["subsections"]:
            sub_key = subsection["title"]
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
            pool.submit(fetch_feed, name, url, fallbacks=fallbacks, max_items_per_source=max_items_per_source, max_age_days=max_age_days): (name, url, fallbacks, sub_key)
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
        # Fresh run: fetch feeds in parallel, then deduplicate in order
        seen_this_run: list[dict] = []

        # Initialise every subsection key so empty subsections still appear
        for section in SECTIONS:
            for subsection in section["subsections"]:
                subsection_articles[subsection["title"]] = []

        feed_results = fetch_all_feeds(SECTIONS, max_items_per_source=tuning["max_items_per_source"], max_age_days=tuning["max_age_days"])

        for section in SECTIONS:
            for subsection in section["subsections"]:
                sub_key = subsection["title"]
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
            print("  ✗ No articles to publish, skipping edition.")
            return False

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
        pt_date_str = post_now.strftime("%Y-%m-%d")
        article_fingerprint = _big_picture_fingerprint(all_articles)

        cached = load_big_picture_cache(site_root, pt_date_str)
        if cached and cached.get("fingerprint") == article_fingerprint:
            logging.info(
                f"Reusing 'The Big Picture' from cache for {pt_date_str} "
                f"(fingerprint {article_fingerprint} matches)."
            )
            global_summary_text = cached["summary_text"]
            global_summary_html = cached["summary_html"]
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
                response = _query_ollama(full_prompt, model)
                return response or "Global summary could not be generated."

            global_summary_text = get_global_summary(all_articles, site_root, config)
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

        html_lines.append('<div style="background: #f9f9f9; padding: 15px; border-left: 5px solid #ccc; margin-bottom: 20px;">')
        html_lines.append('  <h3 style="margin-top:0;">🌍 The Big Picture</h3>')
        if "big-picture" in audio_paths:
            html_lines.append(f'  {audio_player_html(audio_paths["big-picture"], "Big Picture summary")}')
        html_lines.append(f'  <p>{global_summary_html}</p>')
        html_lines.append('</div>')
        html_lines.append("")


    # Build the (section_title, section_articles) job list once, in the
    # canonical SECTIONS order. We dispatch all summaries concurrently
    # below, then iterate SECTIONS again to render — that way output
    # order is preserved even though the LLM calls run in parallel.
    section_jobs: list[tuple[str, list[dict]]] = []
    section_articles_by_title: dict[str, list[dict]] = {}
    for section in SECTIONS:
        section_articles: list[dict] = []
        for subsection in section["subsections"]:
            sub_key = subsection["title"]
            section_articles.extend(subsection_articles.get(sub_key, []))
        if not section_articles:
            # Skip empty sections entirely — no summary, no HTML block.
            continue
        section_articles_by_title[section["title"]] = section_articles
        section_jobs.append((section["title"], section_articles))

    section_summaries = summarize_sections_concurrent(
        section_jobs, site_root, config
    )

    # Generate TTS audio files for each section and Big Picture
    audio_paths = generate_edition_audio(
        edition, site_root,
        global_summary_text if all_articles else None,
        section_summaries, config,
    )

    for section in SECTIONS:
        section_articles = section_articles_by_title.get(section["title"])
        if not section_articles:
            continue

        summary_text = section_summaries.get(
            section["title"],
            "Summary could not be generated.",
        )

        # 3. Linkify the summary text
        summary_html = linkify_summary(summary_text, section_articles)

        # 4. Add section heading and summary to HTML
        html_lines.append("<h2>{}</h2>".format(section["title"]))
        section_slug = _slugify(section["title"])
        if section_slug in audio_paths:
            html_lines.append(audio_player_html(audio_paths[section_slug], f"{section['title']} summary"))
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

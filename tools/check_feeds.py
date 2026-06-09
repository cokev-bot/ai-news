#!/usr/bin/env python3
"""RSS Feed Health Monitor — checks all feeds in sections.json and alerts via
Discord webhook when a feed has been failing for 3+ consecutive runs.

Usage:
    python3 tools/check_feeds.py [SITE_ROOT] [--dry-run] [--json]

    SITE_ROOT   Path to the AI news site root (default: script's parent dir)
    --dry-run   Fetch feeds and update health state, but skip Discord alerts
    --json      Output JSON summary of feed statuses to stdout

The script maintains a per-feed health state in ``.feed_health.json`` inside
SITE_ROOT. Each entry tracks the number of consecutive failures and the
timestamp of the last failure. When ``consecutive_failures >= 3``, a Discord
alert is sent (if ``DISCORD_WEBHOOK_URL`` is set in the environment).

Designed to run as a standalone cron job, independent of the edition pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.error
import socket
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Reuse generate_news feed-fetching logic
# ---------------------------------------------------------------------------

# Add SITE_ROOT's parent to sys.path so we can import generate_news
SITE_ROOT_DEFAULT = str(Path(__file__).resolve().parent.parent)
if SITE_ROOT_DEFAULT not in sys.path:
    sys.path.insert(0, SITE_ROOT_DEFAULT)

from generate_news import (
    _http_get_with_retry,
    _looks_like_rss,
    load_config,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALERT_THRESHOLD = 3  # consecutive failures before alerting
HEALTH_FILE = ".feed_health.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("check_feeds")

# ---------------------------------------------------------------------------
# Health state management
# ---------------------------------------------------------------------------


def load_health(site_root: Path) -> dict:
    """Load ``.feed_health.json`` from SITE_ROOT.

    Returns a dict of ``{feed_name: {url, consecutive_failures, last_success, last_failure, last_error}}``.
    Missing or corrupt file returns an empty dict (first-run / clean-slate).
    """
    path = site_root / HEALTH_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load feed health file: {e}")
    return {}


def save_health(site_root: Path, health: dict) -> None:
    """Write ``.feed_health.json`` atomically."""
    path = site_root / HEALTH_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(health, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Feed checking
# ---------------------------------------------------------------------------


def get_all_feeds(sections: list[dict]) -> list[tuple[str, str, list[str]]]:
    """Extract every (name, primary_url, fallbacks) from sections.json.

    Returns a flat list so the caller can iterate and check each feed.
    """
    feeds = []
    for section in sections:
        for subsection in section.get("subsections", []):
            alts_map = subsection.get("feeds_alts", {}) or {}
            for feed_name, feed_url in subsection.get("feeds", {}).items():
                fallbacks = alts_map.get(feed_name, []) or []
                feeds.append((feed_name, feed_url, fallbacks))
    return feeds


def check_feed(name: str, url: str, fallbacks: list[str] | None = None) -> tuple[bool, str]:
    """Check whether a feed URL returns a valid RSS/Atom body.

    Uses the same ``_http_get_with_retry`` logic as the edition pipeline
    so health checks are consistent with production fetches.

    Returns ``(ok, message)`` where *ok* is True on success and *message*
    is a human-readable status string.
    """
    fallbacks = list(fallbacks or [])
    candidates = [url] + fallbacks
    for idx, candidate in enumerate(candidates):
        raw = _http_get_with_retry(candidate, timeout=15, attempts=1)
        if raw is not None:
            if idx > 0:
                return True, f"OK (fallback #{idx}: {candidate})"
            return True, "OK"
        if idx == 0 and fallbacks:
            log.info(f"{name}: primary failed, trying {len(fallbacks)} fallback(s)")
    return False, f"all {len(candidates)} URL(s) failed"


# ---------------------------------------------------------------------------
# Discord alert
# ---------------------------------------------------------------------------


def send_discord_alert(webhook_url: str, content: str) -> bool:
    """Post *content* to a Discord webhook. Returns True on success."""
    try:
        payload = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        log.error(f"Discord webhook failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def check_all_feeds(
    site_root: Path,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Check every feed and return a list of status dicts.

    Each dict has keys: name, url, ok, message, consecutive_failures,
    alerted (True if an alert was sent or would have been sent).
    """
    sections_path = site_root / "sections.json"
    if not sections_path.exists():
        log.error(f"sections.json not found at {sections_path}")
        return []

    sections = json.loads(sections_path.read_text(encoding="utf-8"))
    all_feeds = get_all_feeds(sections)
    health = load_health(site_root)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    results = []
    alerted_feeds = []

    now_iso = datetime.now(timezone.utc).isoformat()

    for name, url, fallbacks in all_feeds:
        ok, message = check_feed(name, url, fallbacks)
        entry = health.get(name, {
            "url": url,
            "consecutive_failures": 0,
            "last_success": None,
            "last_failure": None,
            "last_error": None,
        })
        # Ensure URL is current (feeds.json may have changed)
        entry["url"] = url

        if ok:
            entry["consecutive_failures"] = 0
            entry["last_success"] = now_iso
            entry["last_error"] = None
        else:
            entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
            entry["last_failure"] = now_iso
            entry["last_error"] = message

        health[name] = entry
        alert = False

        if not ok and entry["consecutive_failures"] >= ALERT_THRESHOLD:
            alert = True
            alert_msg = (
                f"⚠️ **Feed failing {entry['consecutive_failures']}x**: "
                f"**{name}** — {message}"
            )
            if webhook_url and not dry_run:
                send_discord_alert(webhook_url, alert_msg)
            elif dry_run:
                log.info(f"[dry-run] Would alert: {alert_msg}")
            else:
                log.warning(f"No DISCORD_WEBHOOK_URL set; skipping Discord alert: {alert_msg}")
            alerted_feeds.append(name)

        results.append({
            "name": name,
            "url": url,
            "ok": ok,
            "message": message,
            "consecutive_failures": entry["consecutive_failures"],
            "alerted": alert,
        })

        status = "✓" if ok else f"✗ ({entry['consecutive_failures']}x)"
        log.info(f"  {name}: {status} — {message}")

    save_health(site_root, health)

    if alerted_feeds:
        log.warning(f"Alerted feeds: {', '.join(alerted_feeds)}")
    else:
        log.info("All feeds healthy — no alerts.")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Check RSS feed health and alert on persistent failures")
    parser.add_argument("site_root", nargs="?", default=SITE_ROOT_DEFAULT,
                        help="Path to the AI news site root")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check feeds and update state but skip Discord alerts")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON summary to stdout")
    args = parser.parse_args()

    site_root = Path(args.site_root).resolve()
    if not site_root.exists():
        log.error(f"Site root does not exist: {site_root}")
        sys.exit(1)

    log.info(f"Checking feeds from {site_root}/sections.json ...")
    results = check_all_feeds(site_root, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(results, indent=2))

    failing = [r for r in results if not r["ok"]]
    if failing:
        log.warning(f"{len(failing)}/{len(results)} feeds failing")
        sys.exit(1)
    else:
        log.info(f"All {len(results)} feeds healthy")
        sys.exit(0)


if __name__ == "__main__":
    main()
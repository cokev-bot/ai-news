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
import xml.etree.ElementTree as ET
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
    _load_feed_health,
    _looks_like_rss,
    _save_feed_health,
    fetch_feed,
    load_config,
    record_feed_health,
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
    Delegates to ``generate_news._load_feed_health`` so the monitor and the
    pipeline share one read implementation.
    """
    return _load_feed_health(site_root)


def save_health(site_root: Path, health: dict) -> None:
    """Write ``.feed_health.json`` atomically.

    Retained for callers that need to persist a raw dict (e.g. tests). The
    monitor itself no longer uses this: it merges through
    ``generate_news.record_feed_health()`` so the pipeline and the monitor can
    never disagree about a feed's failure streak. Delegates to
    ``generate_news._save_feed_health``.
    """
    _save_feed_health(site_root, health)


# ---------------------------------------------------------------------------
# Feed checking
# ---------------------------------------------------------------------------


def extract_sections(sections_data) -> list[dict]:
    """Normalize ``sections.json`` content into the sections array.

    ``sections.json`` has two valid shapes:

      * legacy: a flat list of sections
      * current: ``{"sections": [...], "source_urls": {...}}``

    Iterating the current shape without normalizing yields the *string keys*
    (``"source_urls"``, ``"sections"``) instead of section dicts, which then
    blows up on ``section.get(...)``. Accept both so the monitor keeps working
    across the format change.
    """
    if isinstance(sections_data, list):
        return sections_data
    if isinstance(sections_data, dict):
        sections = sections_data.get("sections", [])
        return sections if isinstance(sections, list) else []
    return []


def get_all_feeds(sections: list[dict]) -> list[tuple[str, str, list[str]]]:
    """Extract every (name, primary_url, fallbacks) from sections.json.

    Returns a flat list so the caller can iterate and check each feed.
    """
    feeds = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        for subsection in section.get("subsections", []) or []:
            if not isinstance(subsection, dict):
                continue
            alts_map = subsection.get("feeds_alts", {}) or {}
            for feed_name, feed_url in (subsection.get("feeds", {}) or {}).items():
                fallbacks = alts_map.get(feed_name, []) or []
                feeds.append((feed_name, feed_url, fallbacks))
    return feeds


def _parses_as_feed(raw: bytes) -> tuple[bool, str]:
    """Check that *raw* is real, parseable feed XML with at least one item.

    HTTP 200 is not success. Failure modes that pass the size/shape check in
    ``_looks_like_rss`` but deliver nothing usable:

      * leading whitespace before the XML declaration (xcancel.com emits
        ``b'  <?xml'``), which makes expat reject the document outright;
      * a whitelist/placeholder feed whose only item is a "not yet
        whitelisted" notice dated 1971;
      * an error page that happens to contain ``<rss``;
      * a feed that parses but has no items at all.

    The parse is deliberately *not* lenient (no ``lstrip``): it must mirror
    ``generate_news.fetch_feed``, which calls ``ET.fromstring(raw)`` directly.
    If the pipeline would get zero items from this body, the monitor must not
    call it healthy — otherwise it reports a dead source as OK. Assert on
    *usable items*, not on fetch success.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return False, f"unparseable XML: {exc}"
    # RSS uses <item>, Atom uses <entry>. Counting only <item> marks every
    # valid Atom feed as broken, which is just as misleading as the reverse.
    # Match on the local tag name because Atom (and some RSS) declare a default
    # XML namespace, so ".//entry" would silently match nothing.
    items = [
        el for el in root.iter()
        if isinstance(el.tag, str) and el.tag.split("}")[-1] in ("item", "entry")
    ]
    if not items:
        return False, "parsed but contains 0 items"
    # A placeholder feed is technically valid RSS; treat its marker title as a
    # failure so it is not counted as a healthy source.
    for item in items:
        title_el = item.find("title")
        title = (title_el.text or "").lower() if title_el is not None else ""
        if "not yet whitelisted" in title or "rss reader not yet" in title:
            return False, "placeholder feed (reader not whitelisted)"
    return True, "OK"


def check_feed(name: str, url: str, fallbacks: list[str] | None = None) -> tuple[bool, str]:
    """Check whether a feed URL returns a valid, usable RSS/Atom body.

    Delegates to ``generate_news.fetch_feed`` — the exact code the edition
    pipeline uses — so the monitor and the pipeline can never disagree about
    whether a source works. That matters because the monitor previously had its
    own fetch logic and reported "34 feeds healthy" while 26 X feeds were dead.

    Returns ``(ok, message)`` where *ok* is True on success and *message* is a
    human-readable status string.
    """
    fallbacks = list(fallbacks or [])
    try:
        articles = fetch_feed(name, url, fallbacks=fallbacks, max_age_days=1)
        if articles:
            return True, "OK"
        # No items within the window is not a fetch failure, but we still want
        # to distinguish "reachable, quiet" from "unreachable". Probe the raw
        # body to tell them apart.
        raw = _http_get_with_retry(url, timeout=20, attempts=1)
        if raw is None:
            for idx, fb in enumerate(fallbacks, start=1):
                raw = _http_get_with_retry(fb, timeout=20, attempts=1)
                if raw is not None:
                    return True, f"OK (fallback #{idx}: {fb})"
            return False, f"all {len(fallbacks) + 1} URL(s) failed"
        usable, detail = _parses_as_feed(raw)
        if not usable:
            return False, detail
        return True, "OK (no items in window)"
    except Exception as e:  # pragma: no cover - fetch_feed never raises
        return False, f"{type(e).__name__}: {e}"


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
    alerts_only: bool = False,
) -> list[dict]:
    """Check every feed and return a list of status dicts.

    Each dict has keys: name, url, ok, message, consecutive_failures,
    alerted (True if an alert was sent or would have been sent).

    With *alerts_only* no feeds are fetched. Instead the persisted
    ``.feed_health.json`` is read and alerts are raised from it. Use this when
    a fetch has *already* happened in the same job — the edition pipeline
    fetches all 34 feeds and records health before dedup on every run, so
    re-fetching here would double-count each failure and burn 34 extra
    requests three times a day.

    Alert de-duplication: an alert fires once per worsening streak, so a feed
    that stays broken does not re-alert on every subsequent run. The streak
    must recover first, or grow, before it alerts again. Without this, wiring
    the monitor into the three-daily edition crons would produce alert spam.
    """
    sections_path = site_root / "sections.json"
    if not sections_path.exists():
        log.error(f"sections.json not found at {sections_path}")
        return []

    sections = json.loads(sections_path.read_text(encoding="utf-8"))
    all_feeds = get_all_feeds(extract_sections(sections))

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    results = []
    alerted_feeds = []

    if alerts_only:
        # Read-only path: alert from state the pipeline already wrote.
        health = load_health(site_root)
        for name, url, fallbacks in all_feeds:
            entry = health.get(name) or {}
            failures = int(entry.get("consecutive_failures") or 0)
            ok = failures == 0 and bool(entry.get("last_success"))
            message = "OK" if ok else (entry.get("last_error") or "no health data")
            results.append({
                "name": name,
                "url": url,
                "ok": ok,
                "message": message,
                "consecutive_failures": failures,
                "alerted": False,
            })
        for res in results:
            name = res["name"]
            entry = health.get(name) or {}
            failures = res["consecutive_failures"]
            if res["ok"] or failures < ALERT_THRESHOLD:
                continue
            if failures <= int(entry.get("alerted_at_failures") or 0):
                continue  # already alerted for this streak
            alert_msg = (
                f"⚠️ **Feed failing {failures}x**: **{name}** — {res['message']}"
            )
            if webhook_url and not dry_run:
                send_discord_alert(webhook_url, alert_msg)
            elif dry_run:
                log.info(f"[dry-run] Would alert: {alert_msg}")
            else:
                log.warning(f"No DISCORD_WEBHOOK_URL set; skipping Discord alert: {alert_msg}")
            entry["alerted_at_failures"] = failures
            health[name] = entry
            res["alerted"] = True
            alerted_feeds.append(name)
        # A dry run must not consume the alert: persisting the marker here
        # would suppress the next real run's alert for the same streak, i.e.
        # a test would silence a production warning.
        if alerted_feeds and not dry_run:
            save_health(site_root, health)
        failing = [r for r in results if not r["ok"]]
        if failing:
            log.warning(
                f"{len(failing)}/{len(results)} feeds failing: "
                f"{', '.join(r['name'] for r in failing)}"
            )
        else:
            log.info(f"All {len(results)} feeds healthy.")
        if alerted_feeds:
            log.warning(f"Alerted (>= {ALERT_THRESHOLD} consecutive): {', '.join(alerted_feeds)}")
        return results

    # Collect this run's outcomes and merge them through the same
    # record_feed_health() the edition pipeline uses. Both writers must share
    # one implementation: if the monitor and the pipeline each maintained
    # .feed_health.json themselves, running both would double-count every
    # failure, and the consecutive_failures counter that gates alerting would
    # drift away from reality.
    run_results: list[dict] = []

    for name, url, fallbacks in all_feeds:
        ok, message = check_feed(name, url, fallbacks)
        run_results.append({
            "name": name,
            "url": url,
            "ok": ok,
            "error": None if ok else message,
        })

    record_feed_health(site_root, run_results)

    # Re-read so the alert decisions and the report below reflect what was
    # actually persisted, including streaks carried in from earlier runs.
    health = load_health(site_root)

    for res in run_results:
        name, url, ok, message = res["name"], res["url"], res["ok"], res["error"] or "OK"
        entry = health.get(name) or {
            "url": url,
            "consecutive_failures": 0,
            "last_success": None,
            "last_failure": None,
            "last_error": None,
        }
        alert = False

        if not ok and entry.get("consecutive_failures", 0) >= ALERT_THRESHOLD:
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
            "consecutive_failures": entry.get("consecutive_failures", 0),
            "alerted": alert,
        })

        status = "✓" if ok else f"✗ ({entry.get('consecutive_failures', 0)}x)"
        log.info(f"  {name}: {status} — {message}")

    # Report on *failing feeds*, not on *alerted feeds*. These differ whenever a
    # feed is failing below ALERT_THRESHOLD, or when no Discord webhook is
    # configured: the old wording logged "All feeds healthy — no alerts." on a
    # run where 27/34 feeds had just failed to fetch, because nothing crossed
    # the alert threshold. A health monitor that cheerfully reports success
    # during an outage is worse than no monitor.
    failing = [r for r in results if not r["ok"]]
    if failing:
        log.warning(
            f"{len(failing)}/{len(results)} feeds failing: "
            f"{', '.join(r['name'] for r in failing)}"
        )
    else:
        log.info(f"All {len(results)} feeds healthy.")

    if alerted_feeds:
        log.warning(f"Alerted (>= {ALERT_THRESHOLD} consecutive): {', '.join(alerted_feeds)}")

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
    parser.add_argument("--alerts-only", action="store_true",
                        help="Do not fetch; alert from persisted .feed_health.json. "
                             "Use after the edition pipeline has already fetched.")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON summary to stdout")
    args = parser.parse_args()

    site_root = Path(args.site_root).resolve()
    if not site_root.exists():
        log.error(f"Site root does not exist: {site_root}")
        sys.exit(1)

    log.info(f"Checking feeds from {site_root}/sections.json ...")
    results = check_all_feeds(site_root, dry_run=args.dry_run,
                              alerts_only=args.alerts_only)

    if args.json:
        print(json.dumps(results, indent=2))

    failing = [r for r in results if not r["ok"]]
    if failing:
        log.warning(f"{len(failing)}/{len(results)} feeds failing")
        # --alerts-only is advisory: it reads state that a prior step already
        # wrote, so it must not turn a successful edition run into a failure.
        # The edition's own exit code is the authority on the edition.
        sys.exit(0 if args.alerts_only else 1)
    else:
        log.info(f"All {len(results)} feeds healthy")
        sys.exit(0)


if __name__ == "__main__":
    main()
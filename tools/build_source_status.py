#!/usr/bin/env python3
"""Build a public "Source Status" page for the AI News site.

Answers two questions per news source, which are otherwise invisible:

  1. **Last new story** — the most recent time an item from this source was
     actually accepted into a published edition. Derived from
     ``.news_state.json`` (``seen_links[*].feed`` / ``seen_at``), which is
     written by ``generate_news.py`` only for items that survive dedup and the
     age filter.
  2. **Last successful fetch** — the most recent time the feed was fetched and
     returned a usable body. Derived from ``.feed_health.json``, written by
     ``tools/check_feeds.py``.

Reads:
  sections.json      - canonical feed list (name, url, fallbacks, section)
  config.json        - ``tuning.max_age_days`` for the staleness window
  .news_state.json   - per-feed last accepted item (gitignored, runtime)
  .feed_health.json  - per-feed fetch health (gitignored, runtime)

Writes:
  <site_root>/source-status.html - a Jekyll page (front matter + table)

Usage:
    python3 tools/build_source_status.py [SITE_ROOT]

Exits 0 on success. Never raises for missing/garbled state files: a missing
health file yields ``unknown`` rows rather than an aborted edition.

Runs as the last step of ``run_edition.sh``, after a successful Jekyll build,
so the page reflects the same data the just-published edition was built from.
"""

from __future__ import annotations

import html
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITE_ROOT_DEFAULT = str(Path(__file__).resolve().parent.parent)
OUTPUT_FILE = "source-status.html"
STATE_FILE = ".news_state.json"
HEALTH_FILE = ".feed_health.json"
SECTIONS_FILE = "sections.json"
CONFIG_FILE = "config.json"

MAX_AGE_DAYS_DEFAULT = 7

# Status vocabulary. Each value maps to a reader-actionable meaning:
STATUS_OK = "ok"                     # fetched recently, new stories inside the window
STATUS_QUIET = "quiet"               # fetched fine, but nothing new inside the window
STATUS_DEGRADED = "degraded"         # 1-2 consecutive fetch failures
STATUS_FAILING = "failing"           # >= 3 consecutive fetch failures
STATUS_STALE_CHECK = "stale-check"   # no failures recorded, but check is >48h old
STATUS_UNKNOWN = "unknown"           # never fetched successfully

# A fetch is "recent" if it succeeded within this many hours.
FRESH_CHECK_HOURS = 48

STATUS_LABELS = {
    STATUS_OK: "OK",
    STATUS_QUIET: "Quiet",
    STATUS_DEGRADED: "Degraded",
    STATUS_FAILING: "Failing",
    STATUS_STALE_CHECK: "Stale check",
    STATUS_UNKNOWN: "Unknown",
}

STATUS_NOTES = {
    STATUS_OK: "Fetched recently and delivered stories within the window.",
    STATUS_QUIET: "Fetched successfully but no new stories inside the window.",
    STATUS_DEGRADED: "Recent fetch failures, still occasionally serving.",
    STATUS_FAILING: "Three or more consecutive fetch failures.",
    STATUS_STALE_CHECK: "No recent health check recorded for this feed.",
    STATUS_UNKNOWN: "Never fetched successfully since health tracking began.",
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime, or None.

    Returns None for missing/unparsable values and naive datetimes (we never
    compare a naive timestamp against an aware "now").
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def humanize_age(iso_ts: object, now: datetime) -> tuple[str, str]:
    """Return ``(short_age, absolute_iso)`` for an ISO timestamp.

    ``short_age`` is a compact relative label ("just now", "3h ago", "6d ago")
    or ``"never"`` when the timestamp is missing. ``absolute_iso`` is the raw
    UTC timestamp for the cell's ``title`` tooltip ("" when missing).
    """
    dt = parse_iso(iso_ts)
    if dt is None:
        return "never", ""
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        return "just now", dt.astimezone(timezone.utc).isoformat()
    if secs < 90:
        label = "just now"
    elif secs < 3600:
        label = f"{int(secs // 60)}m ago"
    elif secs < 86400:
        label = f"{int(secs // 3600)}h ago"
    elif secs < 86400 * 30:
        label = f"{int(secs // 86400)}d ago"
    else:
        label = f"{int(secs // (86400 * 30))}mo ago"
    return label, dt.astimezone(timezone.utc).isoformat()


def format_utc(dt: datetime) -> str:
    """Render a datetime as ``YYYY-MM-DD HH:MM UTC``."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def timestamp_cell(iso_ts: object, now: datetime) -> str:
    """Render an absolute ``YYYY-MM-DD HH:MM UTC`` timestamp cell.

    Absolute rather than relative ("2m ago") on purpose: the page is generated
    once per run and then sits static, so a relative label freezes and lies.
    "2m ago" read an hour later is still "2m ago". A real timestamp is correct
    whenever it is read, and comparable at a glance down the column.

    The row/table age is still expressed by the tooltip, which carries the
    precise ISO value and the elapsed time as of build.
    """
    dt = parse_iso(iso_ts)
    if dt is None:
        return '<span class="ss-never">never</span>'
    absolute = format_utc(dt)
    age, _ = humanize_age(iso_ts, now)
    return (
        f'<span title="{html.escape(dt.isoformat())} '
        f'({html.escape(age)} at build time)">{html.escape(absolute)}</span>'
    )


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def load_json(path: Path, default):
    """Load JSON from *path*, returning *default* if missing or corrupt."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def collect_feeds(sections_data) -> list[dict]:
    """Flatten ``sections.json`` data into an ordered list of feed dicts.

    Handles both the current object format (``{"sections": [...],
    "source_urls": {...}}``) and the legacy flat-list format. Order is
    preserved exactly as authored, then grouped by section when rendered.

    Each returned dict has: name, url, fallbacks, section, subsection, homepage.
    """
    if isinstance(sections_data, list):
        sections, source_urls = sections_data, {}
    elif isinstance(sections_data, dict):
        sections = sections_data.get("sections", []) or []
        source_urls = sections_data.get("source_urls", {}) or {}
    else:
        return []

    feeds: list[dict] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_title = section.get("title", "")
        for subsection in section.get("subsections", []) or []:
            if not isinstance(subsection, dict):
                continue
            alts_map = subsection.get("feeds_alts", {}) or {}
            for feed_name, feed_url in (subsection.get("feeds", {}) or {}).items():
                feeds.append({
                    "name": feed_name,
                    "url": feed_url,
                    "fallbacks": list(alts_map.get(feed_name, []) or []),
                    "section": section_title,
                    "subsection": subsection.get("title", ""),
                    "homepage": source_urls.get(feed_name, ""),
                })
    return feeds


def last_item_times(seen_links: dict, *, now: datetime, window_days: int) -> tuple[dict, Counter]:
    """Return (``{feed: latest seen_at datetime}``, 7-day item counts).

    Only entries with a parsable ``seen_at`` and a ``feed`` name count. Entries
    without a ``feed`` (legacy/unknown) are ignored rather than bucketed.
    """
    latest: dict[str, datetime] = {}
    counts: Counter = Counter()
    cutoff = now - timedelta(days=window_days)
    for info in (seen_links or {}).values():
        if not isinstance(info, dict):
            continue
        feed = info.get("feed")
        if not feed:
            continue
        seen_at = parse_iso(info.get("seen_at"))
        if seen_at is None:
            continue
        if feed not in latest or seen_at > latest[feed]:
            latest[feed] = seen_at
        if seen_at >= cutoff:
            counts[feed] += 1
    return latest, counts


def compute_status(
    *,
    last_item: datetime | None,
    last_success: datetime | None,
    failures: int,
    now: datetime,
    max_age_days: int,
) -> str:
    """Classify a source into one of the STATUS_* vocabulary values."""
    if last_success is None:
        return STATUS_UNKNOWN
    if failures >= 3:
        return STATUS_FAILING
    if failures > 0:
        return STATUS_DEGRADED
    if (now - last_success).total_seconds() > FRESH_CHECK_HOURS * 3600:
        return STATUS_STALE_CHECK
    if last_item is None:
        return STATUS_QUIET
    if (now - last_item).total_seconds() > max_age_days * 86400:
        return STATUS_QUIET
    return STATUS_OK


def build_rows(
    feeds: list[dict],
    seen_links: dict,
    health: dict,
    *,
    now: datetime | None = None,
    max_age_days: int = MAX_AGE_DAYS_DEFAULT,
    item_window_days: int = MAX_AGE_DAYS_DEFAULT,
) -> list[dict]:
    """Join the feed list with state + health into render-ready rows."""
    if now is None:
        now = datetime.now(timezone.utc)
    latest, counts = last_item_times(seen_links, now=now, window_days=item_window_days)

    rows: list[dict] = []
    for feed in feeds:
        name = feed["name"]
        entry = health.get(name) if isinstance(health, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        last_success = parse_iso(entry.get("last_success"))
        last_failure = parse_iso(entry.get("last_failure"))
        failures = entry.get("consecutive_failures") or 0
        if not isinstance(failures, int):
            try:
                failures = int(failures)
            except (TypeError, ValueError):
                failures = 0
        last_item = latest.get(name)

        rows.append({
            "name": name,
            "url": feed["url"],
            "homepage": feed.get("homepage", ""),
            "section": feed.get("section", ""),
            "subsection": feed.get("subsection", ""),
            "fallbacks": len(feed.get("fallbacks", [])),
            "last_item": last_item,
            "item_count": counts.get(name, 0),
            "last_success": last_success,
            "last_failure": last_failure,
            "last_error": entry.get("last_error"),
            "failures": failures,
            "status": compute_status(
                last_item=last_item,
                last_success=last_success,
                failures=failures,
                now=now,
                max_age_days=max_age_days,
            ),
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    """Count rows per status and how many sources are actively delivering."""
    counts = Counter(r["status"] for r in rows)
    summary = {status: counts.get(status, 0) for status in STATUS_LABELS}
    summary["total"] = len(rows)
    summary["delivering"] = len([r for r in rows if r["status"] == STATUS_OK])
    return summary


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _source_cell(row: dict) -> str:
    """Source name, linked to its homepage when one is known."""
    name = html.escape(row["name"])
    if row["homepage"]:
        return f'<a href="{html.escape(row["homepage"], quote=True)}">{name}</a>'
    return name


def render_rows_html(rows: list[dict], *, now: datetime) -> str:
    """Render the table body, inserting a group header per section."""
    lines: list[str] = []
    current_section = None
    for row in rows:
        if row["section"] != current_section:
            current_section = row["section"]
            label = html.escape(current_section or "Other")
            lines.append(
                f'<tr class="ss-group"><th colspan="6" scope="colgroup">{label}</th></tr>'
            )
        item_cell = timestamp_cell(
            row["last_item"].isoformat() if row["last_item"] else None, now
        )
        fetch_cell = timestamp_cell(
            row["last_success"].isoformat() if row["last_success"] else None, now
        )
        status: str = str(row["status"] or STATUS_UNKNOWN)
        status_label = STATUS_LABELS.get(status, status)
        note = STATUS_NOTES.get(status, "")
        if row.get("last_error"):
            note = f"{note} Last error: {row['last_error']}" if note else str(row["last_error"])
        lines.append(
            '<tr class="ss-row ss-{status}">'
            '<td class="ss-source">{source}</td>'
            '<td class="ss-section">{section}</td>'
            '<td class="ss-time">{item}</td>'
            '<td class="ss-time">{fetch}</td>'
            '<td class="ss-count">{count}</td>'
            '<td class="ss-status"><span class="ss-badge ss-badge-{status}" '
            'title="{note}">{label}</span></td>'
            '</tr>'.format(
                status=html.escape(status, quote=True),
                source=_source_cell(row),
                section=html.escape(row["subsection"] or row["section"] or ""),
                item=item_cell,
                fetch=fetch_cell,
                count=row["item_count"],
                note=html.escape(note, quote=True),
                label=html.escape(status_label),
            )
        )
    return "\n".join(lines)


def render_page(
    rows: list[dict],
    *,
    now: datetime | None = None,
    max_age_days: int = MAX_AGE_DAYS_DEFAULT,
) -> str:
    """Render the complete Jekyll page (front matter + HTML)."""
    if now is None:
        now = datetime.now(timezone.utc)
    summary = summarize(rows)
    body = render_rows_html(rows, now=now)
    updated = format_utc(now)

    return f"""---
layout: page
title: Source Status
permalink: /source-status/
---

<p class="ss-intro">
  Every feed this digest watches, and when it last did something. All times are
  UTC.
  <strong>Last new story</strong> is the most recent item from that source that
  survived de-duplication and made it into a published edition.
  <strong>Last successful fetch</strong> is the most recent time the pipeline
  pulled the feed and got a usable response back — a source can be fetched
  successfully and still have no new story.
  Sources are marked <em>quiet</em> when they are reachable but have not
  delivered a story in {max_age_days} days.
</p>

<p class="ss-summary">
  <strong>{summary['total']}</strong> sources tracked ·
  <strong>{summary['delivering']}</strong> delivering stories ·
  <strong>{summary['quiet']}</strong> quiet ·
  <strong>{summary['failing'] + summary['degraded']}</strong> with fetch failures ·
  updated {html.escape(updated)}
</p>

<style>
.source-status-table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.9em; }}
.source-status-table th, .source-status-table td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #e1e1e1; vertical-align: top; }}
.source-status-table thead th {{ border-bottom: 2px solid #ccc; white-space: nowrap; }}
.source-status-table .ss-group th {{ background: #f4f4f4; font-size: 0.85em; letter-spacing: 0.04em; text-transform: uppercase; }}
.source-status-table .ss-count {{ text-align: right; white-space: nowrap; }}
.source-status-table .ss-time {{ white-space: nowrap; }}
.source-status-table .ss-never {{ color: #999; }}
.source-status-table .ss-badge {{ display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 0.82em; white-space: nowrap; }}
.ss-badge-ok {{ background: #e3f5e6; color: #1c6b2b; }}
.ss-badge-quiet {{ background: #eef0f2; color: #555; }}
.ss-badge-degraded {{ background: #fdf1d8; color: #8a5a00; }}
.ss-badge-failing {{ background: #fbe3e3; color: #a02020; }}
.ss-badge-stale-check {{ background: #fdf1d8; color: #8a5a00; }}
.ss-badge-unknown {{ background: #eef0f2; color: #777; }}
.ss-intro, .ss-summary {{ max-width: 46em; }}
</style>

<table class="source-status-table">
  <thead>
    <tr>
      <th scope="col">Source</th>
      <th scope="col">Section</th>
      <th scope="col">Last new story (UTC)</th>
      <th scope="col">Last successful fetch (UTC)</th>
      <th scope="col">Items ({max_age_days}d)</th>
      <th scope="col">Status</th>
    </tr>
  </thead>
  <tbody>
{body}
  </tbody>
</table>

<p class="ss-footer">
  Feed health is checked independently of the edition pipeline; a feed that is
  <em>quiet</em> may simply have nothing to say. See
  <a href="{{{{ '/about/' | relative_url }}}}">About</a> for the full source list.
</p>
"""


def write_page(site_root: Path, content: str) -> Path:
    """Write the page atomically and return its path."""
    out = site_root / OUTPUT_FILE
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(out)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build(site_root: Path, *, now: datetime | None = None) -> Path:
    """Build the source status page for *site_root* and return its path."""
    if now is None:
        now = datetime.now(timezone.utc)

    sections_data = load_json(site_root / SECTIONS_FILE, None)
    if sections_data is None:
        raise FileNotFoundError(f"{site_root / SECTIONS_FILE} not found or unparsable")

    config = load_json(site_root / CONFIG_FILE, {}) or {}
    tuning = config.get("tuning", {}) if isinstance(config, dict) else {}
    max_age_days = tuning.get("max_age_days", MAX_AGE_DAYS_DEFAULT)
    if not isinstance(max_age_days, int) or max_age_days <= 0:
        max_age_days = MAX_AGE_DAYS_DEFAULT

    state = load_json(site_root / STATE_FILE, {}) or {}
    seen_links = state.get("seen_links", {}) if isinstance(state, dict) else {}
    health = load_json(site_root / HEALTH_FILE, {}) or {}

    feeds = collect_feeds(sections_data)
    rows = build_rows(
        feeds,
        seen_links,
        health,
        now=now,
        max_age_days=max_age_days,
        item_window_days=max_age_days,
    )
    return write_page(site_root, render_page(rows, now=now, max_age_days=max_age_days))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    site_root = Path(argv[1]).resolve() if len(argv) > 1 else Path(SITE_ROOT_DEFAULT).resolve()
    if not site_root.exists():
        print(f"Site root does not exist: {site_root}", file=sys.stderr)
        return 1
    try:
        out = build(site_root)
    except FileNotFoundError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Wrote source status page → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

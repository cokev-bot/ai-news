# AI News Development Roadmap

This file tracks the autonomous evolution of the AI News site.

## 🎯 Strategic Goals
- Transform from a basic digest to a high-signal, visually stunning AI intelligence hub.
- Automate the a-z pipeline from discovery to publication.
- Maintain 100% test coverage for core logic.

## 🗺️ Roadmap

### Phase 1: The "Sleek" Facelift (UI/UX)
- [x] **Custom CSS:** Implement a modern "Dark Mode" aesthetic (Inter font, subtle borders, high contrast). *Reverted per Issue #2 (user preference: Minima defaults). assets/main.scss removed 2026-06-01.*
- [x] **Mobile Optimization:** Refine the layout for mobile readers.
- [x] **Better Visual Hierarchy:** Improve the "Summary" vs "Details" contrast.

### Phase 2: Intelligence Upgrade (LLM/Content)
- [x] **Prompt Iteration:** Move from "summarization" to "insight synthesis" in `summary_prompt.txt`.
- [x] **Global Executive Summary:** Add a top-level "The Big Picture" section for each edition.
- [ ] **Signal Expansion:** Identify and integrate 5+ new high-signal RSS/X sources. *(In progress: Phase 4 #1)*

### Phase 3: Architectural Robustness
- [ ] **Automatic Tagging:** Implement keyword extraction to tag posts (e.g., #LLM, #Agentic).
- [ ] **Advanced Deduplication:** Move beyond Jaccard to embedding-based similarity if needed.

### Phase 4: Reliability & Content (Active — 2026-06-05+)
The current critical path. Items here are addressed by the `ai-news-roadmap-daily` cron (one per day) and discovered by `ai-news-brainstorm-daily`.

- [x] **Nitter fallback chain** — implemented as `_http_get_with_retry` + per-feed fallback chain in `fetch_feed()`. 26 nitter feeds wired with privacydev.net/privacyredirect.com/xcancel.com alternates. *Completed 2026-06-05 (commit feb67b9).*
- [x] **`.news_state.json` no longer tracked in git** — untracked in commit feb67b9. *Completed 2026-06-05.*
- [x] **Per-feed isolation + retry/backoff in `fetch_feed()`** — `_http_get_with_retry` (3 attempts, exponential backoff, RSS body-shape validation) wraps the urllib call; `fetch_feed()` swallows per-feed errors and never aborts the edition. *Completed 2026-06-05 (commit feb67b9).*
- [x] **Parallelize LLM section summaries** — `_query_ollama()` extracted as reusable single-call helper; new `summarize_sections_concurrent()` dispatches section summaries via `concurrent.futures.ThreadPoolExecutor` (capped at `MAX_SUMMARY_WORKERS=6`). `generate_post()` loop refactored to build a `section_jobs` list, dispatch in parallel, and preserve render order. 12 new tests in `tests/test_parallel_summaries.py` confirm parallel execution and correctness. *Completed 2026-06-05 (commit pending).*
- [x] **Reuse "Big Picture" across same-day editions** — `generate_post()` now checks for a per-day `<pt-date>-bp.json` cache (keyed on the `(source,title)` fingerprint of the day's article set) and reuses the cached `summary_text` + pre-rendered `summary_html` for subsequent same-day editions. Cache file is gitignored (`*-bp.json`). Morning edition is canonical; Afternoon/Evening reuse. 18 new tests in `tests/test_big_picture_cache.py`. *Completed 2026-06-05 (commit 2663d30).*
- [x] **Cross-edition dedup tightens from exact-link to last-24h-Jaccard** — `is_duplicate()` now also scans `seen_links` entries with a `seen_at` timestamp inside the last `CROSS_EDITION_DEDUP_HOURS=24` and flags a different-link, similar-title story as a duplicate. New entries stamped `seen_at` at write time; `load_state()` migrates legacy entries (no `seen_at`) to "now" so they fall outside the window and old behavior is preserved. 23 new tests in `tests/test_cross_edition_dedup.py`. *Completed 2026-06-06 (commit 3760fba).*
- [x] **Move `test_clean.py` to `tests/` + add `__main__` guard** — root-level `test_clean.py` renamed to `tests/clean_title.py`; demo loop preserved inside an `if __name__ == "__main__":` guard so `python3 tests/test_logic.py` no longer pollutes stdout. New `tests/test_clean_title.py` adds 30 unit tests (whitespace, every escape char, demo-input regression, and an import-silence test). Existing `test_logic.py` updated to import from the new path. All 124 tests across 10 test files pass. *Completed 2026-06-08 (commit b437836).*
- [ ] **Add RSS feed health monitor** — `tools/check_feeds.py` runs from a separate cron, alerts to Discord when a feed has been failing for 3+ consecutive runs. 2026-06-05.
- [ ] **Add 3 new sources: arXiv cs.AI, HuggingFace Trending Models, The Rundown AI** — current set is X/Twitter-heavy; these add research depth and mainstream press. 2026-06-05.
- [ ] **Fill in `index.markdown`** — home page is empty Jekyll defaults. Show 3 most recent editions with one-liners + a section index. 2026-06-05.
- [ ] **Add site-level RSS feed (`feed.xml`)** — meta, but: an AI news site with no RSS is ironic. 2026-06-05.

### Phase 5: Discovery (Brainstorm Backlog)
Items here were brainstormed by the `ai-news-brainstorm-daily` cron. Each is a candidate for Phase 4 if the daily cron picks it.

- [ ] **Add a per-day "All editions" landing page (`/news/:year/:month/:day/`)** — current site only has per-edition posts and a flat chronological home list. Add a Jekyll `archive.html` layout (or `day.html` permalink) that lists Morning/Afternoon/Evening for a given day, with a one-line tease pulled from each post's "Big Picture". Users can land on a day's overview without scrolling the post list. 2026-06-05.
- [ ] **Add JSON-LD `NewsArticle` schema to generated posts** — `_posts/*.html` has no structured data; Jekyll's SEO plugin only adds WebSite-level schema on the home page. Embed a `<script type="application/ld+json">` block (datePublished, headline from "Big Picture", articleSection, keywords) in `generate_news.py`'s render path so search engines can index individual editions as news articles. 2026-06-05.
- [ ] **Convert source-name labels (`<strong>FT AI</strong>` etc.) into linked source pills** — each item currently renders the source name as a plain bold string. In `generate_news.py`'s post-render step, replace with `<a class="source-pill" href="<source_homepage>">` where `source_homepage` is looked up from a new `source_urls` map in `sections.json`. Improves navigation back to original publishers and gives visual consistency. 2026-06-05.
- [ ] **Add `/archive/` page grouped by month with per-month counts** — the post list grows linearly and has no year/month grouping. Add a Jekyll page that walks `site.posts`, groups by `date | date: '%B %Y'`, and shows count + a list of dated editions. Gives readers (and crawlers) a navigable structure beyond the flat 5-deep list. 2026-06-05.
- [ ] **Expose `TIMEZONE` in `config.json` and read it from both `run_edition.sh` and `pacific_now()`** — `America/Los_Angeles` is hardcoded in two places (`run_edition.sh` `TZ=`, `generate_news.py` `pacific_now()`). Centralize into `config.json` so future contributors in other zones (or DST edge cases) can override without code edits. 2026-06-05.
- [ ] **Parallelize RSS feed fetching alongside the existing LLM parallelization** — `generate_news.py` already parallelizes LLM section summaries via `ThreadPoolExecutor` (Phase 4), but `fetch_feed()` calls run serially. With 31 feeds at ~1–3s each (especially with Nitter fallbacks), that is 30–90s of pure wall-clock waste. Wrap the per-feed fan-out in a second `ThreadPoolExecutor` (separate from the LLM one so they don't share workers), bounded by the same kind of worker cap, with per-feed error isolation preserved. 2026-06-06.
- [ ] **Filter "R to @user:" retweet-only items out of rendered posts** — many X items render as low-signal retweet noise (`R to @antigravity: CLI Walkthrough`). In `render_item()` (or upstream in `fetch_feed()` after Nitter parsing), drop items whose title starts with `R to @` or whose only content is a retweet; tests should cover both. Cuts ~30–50% of clutter in AI Labs / Developers sections. 2026-06-06.
- [ ] **Group home page (`index.markdown`) by date with per-day edition counts instead of a flat 5-deep list** — current home is `<ul class="post-list">` of every edition ever, hard to scan. Switch the override layout in `index.markdown` to walk `site.posts` and group by `date | date: '%B %-d, %Y'`, with each day showing Morning/Afternoon/Evening badges and an item count. Improves both scannability and the "this is a daily digest" framing. 2026-06-06.
- [ ] **Add `jekyll-sitemap` plugin and ship a `sitemap.xml`** — search engines currently get the home page and post pages from crawls but no explicit sitemap; the `plugins:` list in `_config.yml` already includes `jekyll-feed` so adding `jekyll-sitemap` is one line + a Gemfile bump. Faster indexing of new editions, which currently can take 24–48h to appear in Google. 2026-06-06.
- [ ] **Add log rotation to `generate_news.log`** — the file grows unbounded (already shows three 20:00/00:00/15:01 cycles per day with INFO-level per-feed + section lines; will hit hundreds of MB in months). Add a `RotatingFileHandler` (or a tiny nightly `logrotate`-style script) bounded to ~10MB × 3 backups, and add `generate_news.log*` to `.gitignore` so rotated files don't accidentally get committed. 2026-06-07.
- [ ] **Move tunable constants (`MAX_ITEMS_PER_SOURCE`, `MAX_AGE_DAYS`, `TITLE_SIM_THRESHOLD`, `CROSS_EDITION_DEDUP_HOURS`) from module-level globals into `config.json`** — these are tuning knobs but currently require editing `generate_news.py` to change. Centralize them in `config.json` next to `model` / `summary_prompt_file` and read them via a new `get_tuning(site_root)` helper, with the existing values as `DEFAULT_CONFIG["tuning"]` so a missing key still works. Tests should cover merge order. 2026-06-07.
- [ ] **Generate a daily social card (OG image) per edition using the "Big Picture" first sentence** — the Jekyll SEO tag already injects WebSite-level `og:title` / `og:description` on the home page, but individual `_posts/*.html` posts have no `og:image` and no `twitter:image` — link previews on X/LinkedIn/Slack show a blank box. Add a `tools/make_og_image.py` (Pillow, no extra dep on most systems) that renders a 1200×630 PNG with the edition title + Big Picture excerpt, called from `run_edition.sh` after `generate_news.py` writes the post, and front-mattered as `image: /ai-news/assets/og/YYYY-MM-DD-<Edition>.png`. New items gitignored; site builds them on demand. 2026-06-07.
- [ ] **Surface feed-level "item age" and "is stale" warnings in the edition's `Scanning N feeds` line** — current front matter is `Scanning 31 feeds · 2 accounts posted · 2 items`, which gives no signal about whether the items are from today or 6 days old. Extend the header line to include a freshness tally (e.g. `· 2 fresh, 0 stale, 1 from yesterday`) computed from `MAX_AGE_DAYS` vs. item `published` timestamp. Catches silent feed-staleness visually without needing `tools/check_feeds.py` cron to alert. 2026-06-07.

---

## 🛠️ Operating Procedures
1. **Issue Sync:** Check GitHub Issues for user feedback → Convert to tasks here.
2. **Dev Cycle:** Pick task → Implement → Run `tests/test_logic.py` → Push.
3. **Communication:** Close GitHub Issues with a summary of the fix/improvement.

## 🤖 Autonomous Maintenance (NEW — 2026-06-05)
Two new cron jobs run daily on this repo's behalf. Both MUST read this file before acting, and both MUST update this file when they change something.

- **`ai-news-roadmap-daily`** (every day) — reads ROADMAP.md, picks exactly ONE item from Phase 4 (highest-priority unchecked item), implements it (per Operating Procedure #2: write tests, run them, commit, push), and marks it `[x]`. Then reports 1-2 lines to Discord via cron auto-delivery. If Phase 4 is empty, picks the top item from Phase 5 instead. SKIPS if the working tree is dirty.
- **`ai-news-brainstorm-daily`** (every day) — reads the current `~/ai-news/`, the live site structure, and this roadmap, brainstorms 3-5 new concrete improvement ideas, and appends them to Phase 5 with today's date. Does NOT implement — only discovers. If Phase 5 has more than 15 unchecked items, SKIPS and reports (backlog is full).

These crons are intentionally separated so that discovery (brainstorm) and execution (roadmap) cannot step on each other.

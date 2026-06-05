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
- [ ] **Reuse "Big Picture" across same-day editions** — currently regenerates per edition even though Morning/Afternoon/Evening for the same PT-day should share the day's narrative summary. Persist a `YYYY-MM-DD-bp.json` and reuse. 2026-06-05.
- [ ] **Cross-edition dedup tightens from exact-link to last-24h-Jaccard** — `is_duplicate()` only checks within an edition. Stories re-reported as "breaking" 12h later slip through. Add a 24h title-similarity check against `seen_links` titles. 2026-06-05.
- [ ] **Move `test_clean.py` to `tests/` + add `__main__` guard** — current location breaks AGENTS.md convention; module-level demo loop pollutes test runner stdout. 2026-06-05.
- [ ] **Add RSS feed health monitor** — `tools/check_feeds.py` runs from a separate cron, alerts to Discord when a feed has been failing for 3+ consecutive runs. 2026-06-05.
- [ ] **Add 3 new sources: arXiv cs.AI, HuggingFace Trending Models, The Rundown AI** — current set is X/Twitter-heavy; these add research depth and mainstream press. 2026-06-05.
- [ ] **Fill in `index.markdown`** — home page is empty Jekyll defaults. Show 3 most recent editions with one-liners + a section index. 2026-06-05.
- [ ] **Add site-level RSS feed (`feed.xml`)** — meta, but: an AI news site with no RSS is ironic. 2026-06-05.

### Phase 5: Discovery (Brainstorm Backlog)
Items here were brainstormed by the `ai-news-brainstorm-daily` cron. Each is a candidate for Phase 4 if the daily cron picks it.

- _(populated by ai-news-brainstorm-daily)_

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

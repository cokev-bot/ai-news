# AI News Repository - Agent Configuration

This repository is a Jekyll-based AI News Digest site that automatically aggregates news from RSS feeds and summarizes them using an LLM.

## 🤖 Agent Role & Workflow

The agent is responsible for the end-to-end pipeline of news collection, summarization, and publishing.

### 1. The Pipeline (`run_edition.sh`)
The core workflow is orchestrated by `run_edition.sh`. It performs the following steps:
1. **Environment Setup:** Sets the timezone to `America/Los_Angeles` to ensure date consistency.
2. **Sync:** Performs a `git pull` to ensure the latest configuration and content are present.
3. **Generation:** Executes `generate_news.py` with the specific edition (Morning/Afternoon/Evening).
4. **Build:** Runs `bundle exec jekyll build` to compile the site.
5. **Publish:** Commits the new HTML post to `_posts/` and pushes it to the GitHub repository.

### 2. News Generation (`generate_news.py`)
The Python engine handles the "intelligence" of the site:
- **Fetching:** Pulls data from sources defined in `sections.json`.
- **Deduplication:** Uses Jaccard similarity and link tracking (via `.news_state.json`) to prevent duplicate stories across editions.
- **Summarization:** Sends a curated list of articles to a local Ollama instance (`gemma4:31b-cloud`) using the prompt defined in `summary_prompt.txt`.
- **Rendering:** Converts raw data and LLM summaries into Jekyll-compatible HTML posts.
- **Text-to-Speech:** Generates MP3 audio for each section and Big Picture summary using `edge-tts`, with inline audio players embedded in posts. Audio files are stored in `assets/audio/<edition>/` and committed to the repo.
- **Feed health:** Each fetch pass records per-feed success/failure into `.feed_health.json` via `record_feed_health()`, so `tools/build_source_status.py` can publish a live `/source-status/` page.

### 3. Source Status Page (`tools/build_source_status.py`)
Generates `source-status.html` (published at `/source-status/`), listing every feed in `sections.json` with the last time it produced a story accepted into an edition (from `.news_state.json`) and the last time it was successfully fetched (from `.feed_health.json`). `run_edition.sh` runs it after `generate_news.py` and before `bundle exec jekyll build`; it is non-fatal, so a failure there never costs an edition.

## 🛠️ Key Configuration Files

- **`sections.json`**: Defines the hierarchy of News $\rightarrow$ Subsections $\rightarrow$ RSS Feeds.
- **`config.json`**: Contains LLM model settings, prompt file paths, tuning overrides, and TTS settings (`tts.voice`, `tts.rate`, `tts.enabled`).
- **`summary_prompt.txt`**: The system prompt used by the LLM to generate section summaries.
- **`.news_state.json`**: Persistent state tracking all previously seen links to ensure unique content.

## 🧪 Testing & Quality Assurance

**Tests must always be written and verified for any changes to the logic or pipeline.**

- **Test Suite:** Located in `tests/`.
- **Running Tests:** Execute from the repo root using:
  ```bash
  PYTHONPATH=. python3 -m unittest discover -s tests
  ```
  Or for a single test file: `PYTHONPATH=. python3 -m unittest tests.test_tts`
- **Requirement:** No code changes should be pushed to the repository without accompanying test updates and a successful test run.

## 📅 Schedule
The site is updated three times daily (UTC):
- **Morning:** 15:00 UTC
- **Afternoon:** 20:00 UTC
- **Evening:** 00:00 UTC

## 🚀 Maintenance Commands
To manually trigger an update for a specific edition:
```bash
/home/ubuntu/ai-news/run_edition.sh <Morning|Afternoon|Evening>
```

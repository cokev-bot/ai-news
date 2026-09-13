#!/bin/bash
set -e
set -o pipefail

EDITION=$1
DRY_RUN=0
if [ "${2:-}" = "--dry-run" ] || [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    EDITION=${1}
    if [ "$EDITION" = "--dry-run" ]; then
        EDITION=${2:-}
    fi
fi

if [ -z "$EDITION" ] || [[ ! "$EDITION" =~ ^(Morning|Afternoon|Evening)$ ]]; then
    echo "Usage: $0 <Morning|Afternoon|Evening> [--dry-run]"
    echo ""
    echo "Env vars:"
    echo "  MANUAL_RUN=1  Use UTC for the post frontmatter date (avoids URL"
    echo "                collisions with cron-driven runs of the same edition)"
    exit 1
fi

# Read timezone from config.json; fall back to America/Los_Angeles
TIMEZONE=$(python3 -c "import json; print(json.load(open('/home/ubuntu/ai-news/config.json')).get('timezone','America/Los_Angeles'))" 2>/dev/null || echo 'America/Los_Angeles')
DATE=$(TZ="$TIMEZONE" date '+%Y-%m-%d')
cd /home/ubuntu/ai-news

# Pin the build host's timezone to the site's timezone. Jekyll uses the local
# timezone to turn each post's frontmatter date into its permalink day and into
# every `| date:` filter output, so on a UTC host an Evening edition (17:00 PT =
# 00:00 UTC next day) published under the future day. _config.yml also sets
# `timezone:`, so Jekyll's own ENV["TZ"] assignment would do this too — setting
# it here as well means the build does not depend on either mechanism alone
# (and covers anything else in this script that shells out to a date command).
export TZ="$TIMEZONE"

# Always publish to main so the live site (https://cokev-bot.github.io/ai-news/)
# picks up every edition. If a previous run left the working tree on a feature
# branch, switch back to main; stash any uncommitted work so we never lose it.
if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
    echo "Working tree is on '$(git rev-parse --abbrev-ref HEAD)' — switching to main."
    if ! git diff --quiet || ! git diff --cached --quiet; then
        STASH_NAME="run_edition-auto-stash-$(date '+%Y%m%d-%H%M%S')"
        echo "Stashing uncommitted changes as '$STASH_NAME' before checkout."
        git stash push -u -m "$STASH_NAME"
    fi
    git checkout main
fi

# Pull latest changes (and fast-forward main) to ensure we have current config
git pull --ff-only origin main

echo "Running $EDITION edition for $DATE (PT)..."
# generate_news.py fetches all feeds and records .feed_health.json before dedup,
# so it refreshes health even on a run that publishes nothing. Its non-zero exit
# (a degraded run, or a genuine error) must NOT stop the steps below: the health
# alert and the status page are most valuable precisely when the edition failed.
# Capture the code, then decide at the end.
set +e
python3 generate_news.py "${DATE}-${EDITION}" /home/ubuntu/ai-news
EDITION_RC=$?
set -e

# Alert on persistently failing feeds, using the health the run above just
# wrote. --alerts-only does not fetch (the pipeline already did), so it neither
# double-counts failures nor re-requests 34 feeds three times a day. Non-fatal
# on purpose: alerting must never cost us an edition or mask its exit code.
if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    python3 tools/check_feeds.py /home/ubuntu/ai-news --alerts-only \
        || echo "[!] Feed health alerting failed (non-fatal)."
else
    echo "[i] DISCORD_WEBHOOK_URL not set; skipping feed failure alerts."
fi

# Regenerate the public /source-status/ page from the state files the run above
# just updated. Must run BEFORE the Jekyll build so the page is compiled into
# _site/. Non-fatal: a failure here must never cost us an edition, it just
# means the status page lags by one run.
python3 tools/build_source_status.py /home/ubuntu/ai-news \
    || echo "[!] Source status page generation failed (non-fatal; edition continues)."

# Regenerate the public /week/ page (this week's Big Picture timeline) from the
# posts on disk, including the one this run just wrote. Non-fatal, like the
# source-status page; a failure only means the page lags by one run.
python3 tools/build_week_page.py /home/ubuntu/ai-news \
    || echo "[!] Week page generation failed (non-fatal; edition continues)."

if [ "$EDITION_RC" != "0" ]; then
    echo "[!] generate_news.py exited $EDITION_RC — no edition to publish."
    exit "$EDITION_RC"
fi

# Run Jekyll build. With pipefail set, a non-zero exit from jekyll (including
# a failed build) will abort the script before we commit and push a broken post.
# Retry twice with a 30s backoff: a transient Bundler/gem/network hiccup should
# not cost an entire edition. Only a persistent failure aborts.
JEKYLL_RC=1
for attempt in 1 2 3; do
    if bundle exec jekyll build --destination _site; then
        JEKYLL_RC=0
        break
    fi
    echo "[!] jekyll build failed (attempt $attempt/3)."
    if [ "$attempt" -lt 3 ]; then
        echo "    Retrying in 30s..."
        sleep 30
    fi
done
if [ "$JEKYLL_RC" != "0" ]; then
    echo "[!] jekyll build failed 3 times — aborting edition."
    exit "$JEKYLL_RC"
fi

if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] Skipping git commit and push. Generated post and build artifacts are in _posts/ and _site/."
    exit 0
fi

# Stage content paths only. .news_state.json is gitignored and will cause
# `git add` to exit 1 even with --ignore-errors, which kills the script
# under `set -e`. The `|| true` guards against any other gitignored paths
# that might slip in.
git add --ignore-errors _posts/ _config.yml assets/ api/ source-status.html week.html now.html || true
if ! git diff --cached --quiet; then
    git commit -m "$EDITION AI News Digest $DATE"
    # Push explicitly to origin/main so the live site updates regardless of
    # any local branch the working tree may have been on before this run.
    git push origin main
else
    echo "No changes to commit (all paths either unchanged or gitignored)."
fi

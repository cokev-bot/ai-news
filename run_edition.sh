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

# Use Pacific Time for edition date so 00:00 UTC (5pm PT) stays on same PT day
DATE=$(TZ='America/Los_Angeles' date '+%Y-%m-%d')
cd /home/ubuntu/ai-news

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
python3 generate_news.py "${DATE}-${EDITION}" /home/ubuntu/ai-news

# Run Jekyll build. With pipefail set, a non-zero exit from jekyll (including
# a failed build) will abort the script before we commit and push a broken post.
bundle exec jekyll build --destination _site

if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] Skipping git commit and push. Generated post and build artifacts are in _posts/ and _site/."
    exit 0
fi

git add _posts/ _config.yml .news_state.json
git commit -m "$EDITION AI News Digest $DATE" || echo "No changes to commit"
# Push explicitly to origin/main so the live site updates regardless of any
# local branch the working tree may have been on before this run.
git push origin main

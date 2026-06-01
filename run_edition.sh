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
    exit 1
fi

# Use Pacific Time for edition date so 00:00 UTC (5pm PT) stays on same PT day
DATE=$(TZ='America/Los_Angeles' date '+%Y-%m-%d')
cd /home/ubuntu/ai-news

# Pull latest changes to ensure we have the current config and content
git pull

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
git push

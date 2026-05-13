#!/bin/bash
set -e

EDITION=$1
if [ -z "$EDITION" ]; then
    echo "Usage: $0 <Morning|Afternoon|Evening>"
    exit 1
fi

# Use Pacific Time for edition date so 00:00 UTC (5pm PT) stays on same PT day
DATE=$(TZ='America/Los_Angeles' date '+%Y-%m-%d')
cd /home/ubuntu/ai-news

# Pull latest changes to ensure we have the current config and content
git pull

echo "Running $EDITION edition for $DATE (PT)..."
python3 generate_news.py "${DATE}-${EDITION}" /home/ubuntu/ai-news
bundle exec jekyll build --destination _site 2>&1 | tail -3
git add _posts/ _config.yml
git commit -m "$EDITION AI News Digest $DATE"
git push

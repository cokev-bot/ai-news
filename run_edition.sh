#!/bin/bash
set -e

EDITION=$1
if [ -z "$EDITION" ]; then
    echo "Usage: $0 <Morning|Afternoon|Evening>"
    exit 1
fi

DATE=$(date -u '+%Y-%m-%d')
cd /home/ubuntu/ai-news

echo "Running $EDITION edition for $DATE..."
python3 generate_news.py "${DATE}-${EDITION}" /home/ubuntu/ai-news
bundle exec jekyll build --destination _site 2>&1 | tail -3
git add _posts/ _config.yml
git commit -m "$EDITION AI News Digest $DATE"
git push

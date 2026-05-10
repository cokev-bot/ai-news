#!/bin/bash
# Build Jekyll site and push to GitHub
set -e
cd /home/ubuntu/ai-news
bundle exec jekyll build --destination _site 2>&1 | tail -3
cd /home/ubuntu/ai-news && git add _posts/ _config.yml && git commit -m "Update AI News Digest $(date -u '+%Y-%m-%d %H:%M UTC')" && git push 2>&1

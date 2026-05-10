#!/bin/bash
cd /home/ubuntu/ai-news

# Generate news
python3 generate_news.py Morning /home/ubuntu/ai-news

# Build site
bundle exec jekyll build --destination _site 2>&1 | tail -3

# Commit and push
git add _posts/ _config.yml
git commit -m "Morning AI News Digest $(date -u '+%Y-%m-%d')"
git push

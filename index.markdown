---
layout: home
---

<h2>AI News Digest</h2>

<p>Daily AI intelligence — updated Morning (8am PT), Afternoon (1pm PT), and Evening (5pm PT).</p>

<h3>Recent Editions</h3>

{% assign recent_posts = site.posts | limit: 15 %}
{% assign current_date = "" %}
{% assign day_count = 0 %}
{% assign prev_day_count = 0 %}

<div class="editions-by-date">
{% for post in recent_posts %}
  {% assign post_date = post.date | date: "%Y-%m-%d" %}
  {% assign post_date_display = post.date | date: "%B %-d, %Y" %}

  {% comment %}Close previous day-group if date changed{% endcomment %}
  {% if current_date != "" and post_date != current_date %}
    {% assign prev_day_count = day_count %}
  </ul>
  </div><!-- close day-group -->
  {% endif %}

  {% comment %}Start new day-group if date changed{% endcomment %}
  {% if post_date != current_date %}
    {% assign current_date = post_date %}
    {% assign day_count = 0 %}
    <div class="day-group">
      <h3 class="day-heading">
        <a href="{{ site.baseurl }}/news/{{ post_date | date: "%Y/%m/%d" }}/">
          {{ post_date_display }}
        </a>
      </h3>
      <ul class="day-editions">
  {% endif %}

  {% assign day_count = day_count | plus: 1 %}

  {% comment %}Determine edition type for badge{% endcomment %}
  {% assign edition_type = "edition" %}
  {% if post.title contains "Morning" %}
    {% assign edition_type = "morning" %}
  {% elsif post.title contains "Afternoon" %}
    {% assign edition_type = "afternoon" %}
  {% elsif post.title contains "Evening" %}
    {% assign edition_type = "evening" %}
  {% endif %}

  <li class="day-edition-item">
    <span class="edition-badge edition-badge-{{ edition_type }}">{{ edition_type | capitalize }}</span>
    <a href="{{ post.url | relative_url }}">{{ post.title }}</a>
  </li>

  {% comment %}Close last day-group at end of loop{% endcomment %}
  {% if forloop.last %}
    </ul>
    </div><!-- close day-group -->
  {% endif %}
{% endfor %}
</div>

<p class="archive-link">Browse all past editions in the <a href="{{ '/news/' | relative_url }}">archive</a> or <a href="{{ '/archive/' | relative_url }}">by month</a>.</p>

<h3>Sections</h3>

<ul>
  <li><strong>News</strong> — Financial Times, New York Times</li>
  <li><strong>AI Labs</strong> — Anthropic, OpenAI, Google, DeepSeek, Mistral, and more</li>
  <li><strong>Developers</strong> — Official developer accounts from major AI labs</li>
  <li><strong>Developer Tools</strong> — Ollama, Google AI Studio, and tools updates</li>
  <li><strong>Benchmarks</strong> — Arena.ai and other evaluation sources</li>
  <li><strong>Under Review</strong> — Independent blogs, researcher feeds</li>
</ul>

<h3>Subscribe</h3>

<p>Get updates in your feed reader: <a href="{{ '/feed.xml' | relative_url }}">RSS Feed</a></p>
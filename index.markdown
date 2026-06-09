---
layout: home
---

<h2>AI News Digest</h2>

<p>Daily AI intelligence — updated Morning (8am PT), Afternoon (1pm PT), and Evening (5pm PT).</p>

<h3>Recent Editions</h3>

{% assign recent = site.posts | limit: 9 %}
<ul class="post-list">
{% for post in recent %}
  <li>
    <time datetime="{{ post.date | date_to_xmlschema }}">{{ post.date | date: "%B %-d, %Y" }}</time>
    — <a href="{{ post.url | relative_url }}">{{ post.title }}</a>
  </li>
{% endfor %}
</ul>

<h3>Sections</h3>

<ul>
  <li><strong>News</strong> — Financial Times, New York Times</li>
  <li><strong>AI Labs</strong> — Anthropic, OpenAI, Google, DeepSeek, Mistral, and more</li>
  <li><strong>Developers</strong> — Official developer accounts from major AI labs</li>
  <li><strong>Developer Tools</strong> — Ollama, Google AI Studio, and tools updates</li>
  <li><strong>Benchmarks</strong> — Arena.ai and other evaluation sources</li>
  <li><strong>Under Review</strong> — Independent blogs, researcher feeds</li>
</ul>

<h3>Archive</h3>

<p>Browse all past editions <a href="{{ '/news/' | relative_url }}">by date</a>.</p>
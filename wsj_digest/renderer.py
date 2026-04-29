"""
Renderer — produces HTML and Markdown output files from the selected and
summarised articles.

Public interface:
    render_html(articles_by_category, category_defs, config, output_dir,
                date_str, tldr, market_data) -> Path
    render_markdown(articles_by_category, category_defs, config, output_dir) -> Path
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, BaseLoader

from .models import Article

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# HTML Template (Jinja2, embedded)                                     #
# ------------------------------------------------------------------ #

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Daily Financial Digest &ndash; {{ date_str }}</title>
  <style>
    /* ---- CSS variables: light mode ---- */
    :root {
      --navy:      #003366;
      --red:       #b30000;
      --bg:        #f5f5f0;
      --card:      #ffffff;
      --text:      #1a1a1a;
      --muted:     #666666;
      --border:    #dddddd;
      --why-bg:    #f0f4f8;
      --sidebar-bg:#ffffff;
      --bull-bg:   #e8f5e9; --bull-fg: #2e7d32; --bull-br: #a5d6a7;
      --bear-bg:   #fce4ec; --bear-fg: #c62828; --bear-br: #ef9a9a;
      --neut-bg:   #f5f5f5; --neut-fg: #555555; --neut-br: #bdbdbd;
      --ticker-bg: #eef2f7;
      --tldr-bg:   #fffbf0;
      --tldr-br:   #e8a000;
    }
    /* ---- Dark mode overrides ---- */
    body.dark-mode {
      --navy:      #6eaeff;
      --red:       #ff7b7b;
      --bg:        #0f1117;
      --card:      #1a1d27;
      --text:      #e2e2e2;
      --muted:     #8a8a9a;
      --border:    #2a2d3a;
      --why-bg:    #1e2235;
      --sidebar-bg:#151720;
      --bull-bg:   #1b3a22; --bull-fg: #66bb6a; --bull-br: #2e7d32;
      --bear-bg:   #3a1a22; --bear-fg: #ef5350; --bear-br: #c62828;
      --neut-bg:   #252535; --neut-fg: #9090a0; --neut-br: #3a3a50;
      --ticker-bg: #1a1d2e;
      --tldr-bg:   #1e1a08;
      --tldr-br:   #8a6a00;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: Georgia, 'Times New Roman', serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
      transition: background 0.25s, color 0.25s;
    }

    /* ---- Dark-mode toggle (fixed) ---- */
    .dm-toggle {
      position: fixed;
      top: 1rem;
      right: 1rem;
      z-index: 1000;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 50%;
      width: 2.4rem;
      height: 2.4rem;
      font-size: 1.1rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 2px 8px rgba(0,0,0,0.15);
      transition: background 0.25s, border-color 0.25s;
      padding: 0;
      line-height: 1;
    }
    .dm-toggle:hover { opacity: 0.85; }

    /* ---- Two-column page grid ---- */
    .page-wrapper {
      display: grid;
      grid-template-columns: 210px minmax(0, 860px);
      gap: 2rem;
      max-width: 1130px;
      margin: 0 auto;
      padding: 2rem 1rem 4rem;
    }

    /* ---- Sticky sidebar ---- */
    .sidebar {
      align-self: start;
      position: sticky;
      top: 1.5rem;
    }
    .sidebar-nav {
      background: var(--sidebar-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.9rem 0.8rem;
      transition: background 0.25s, border-color 0.25s;
    }
    .sidebar-heading {
      font-size: 0.68rem;
      font-family: Arial, sans-serif;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 0.5rem;
      padding-left: 0.4rem;
    }
    .sidebar-link {
      display: block;
      padding: 0.32rem 0.55rem;
      margin-bottom: 0.1rem;
      font-size: 0.82rem;
      font-family: Arial, sans-serif;
      color: var(--text);
      text-decoration: none;
      border-radius: 5px;
      transition: background 0.15s, color 0.15s;
    }
    .sidebar-link:hover { background: var(--border); }
    .sidebar-link.active { background: var(--navy); color: #fff; }

    /* ---- Main content column ---- */
    .main-content { min-width: 0; }

    /* ---- Header ---- */
    header {
      border-bottom: 4px double var(--red);
      padding-bottom: 1rem;
      margin-bottom: 1.5rem;
    }
    header h1 {
      font-size: 2rem;
      color: var(--navy);
      letter-spacing: 0.02em;
    }
    .digest-meta {
      font-size: 0.88rem;
      color: var(--muted);
      font-family: Arial, sans-serif;
      margin-top: 0.4rem;
    }

    /* ---- Market data ticker banner ---- */
    .market-ticker {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.3rem 0;
      background: var(--ticker-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.6rem 0.85rem;
      margin-bottom: 1.4rem;
      font-family: Arial, sans-serif;
      font-size: 0.82rem;
      transition: background 0.25s;
    }
    .ticker-item {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      padding: 0.15rem 0.4rem;
      white-space: nowrap;
    }
    .ticker-name  { color: var(--muted); font-size: 0.74rem; }
    .ticker-price { font-weight: 700; color: var(--text); }
    .ticker-change { font-weight: 600; font-size: 0.76rem; }
    .ticker-item.ticker-up   .ticker-change { color: #2e7d32; }
    .ticker-item.ticker-down .ticker-change { color: #c62828; }
    body.dark-mode .ticker-item.ticker-up   .ticker-change { color: #66bb6a; }
    body.dark-mode .ticker-item.ticker-down .ticker-change { color: #ef5350; }
    .ticker-sep { color: var(--border); padding: 0 0.25rem; user-select: none; }

    /* ---- TL;DR / Today's Brief ---- */
    .tldr-section {
      background: var(--tldr-bg);
      border-left: 5px solid var(--tldr-br);
      border-radius: 0 8px 8px 0;
      padding: 0.85rem 1.15rem;
      margin-bottom: 1.8rem;
      transition: background 0.25s;
    }
    .tldr-label {
      font-size: 0.64rem;
      font-family: Arial, sans-serif;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 0.35rem;
    }
    .tldr-text {
      font-size: 0.95rem;
      line-height: 1.7;
      color: var(--text);
    }

    /* ---- Mobile top-nav pills (hidden on desktop) ---- */
    .category-nav {
      display: none;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-bottom: 2rem;
    }
    .category-nav a {
      background: var(--navy);
      color: #fff;
      text-decoration: none;
      padding: 0.28rem 0.72rem;
      border-radius: 20px;
      font-size: 0.80rem;
      font-family: Arial, sans-serif;
      transition: background 0.15s;
    }
    .category-nav a:hover { background: var(--red); }

    /* ---- Category section ---- */
    .category-section { margin-bottom: 3rem; }
    .category-header {
      font-size: 1.45rem;
      color: var(--navy);
      border-left: 5px solid var(--red);
      padding-left: 0.65rem;
      margin-bottom: 0.3rem;
    }
    .category-desc {
      font-size: 0.85rem;
      color: var(--muted);
      font-style: italic;
      margin-bottom: 1.2rem;
      padding-left: 0.9rem;
    }

    /* ---- Article card (lazy-reveal via JS) ---- */
    .article-card {
      background: var(--card);
      border-left: 4px solid var(--red);
      border-radius: 0 6px 6px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      padding: 1.1rem 1.4rem;
      margin-bottom: 1.3rem;
      opacity: 0;
      transform: translateY(14px);
      transition: opacity 0.35s ease, transform 0.35s ease,
                  background 0.25s, box-shadow 0.25s;
    }
    .article-card.visible { opacity: 1; transform: translateY(0); }

    .article-title {
      font-size: 1.08rem;
      font-weight: bold;
      color: var(--navy);
      text-decoration: none;
      display: block;
      margin-bottom: 0.25rem;
    }
    .article-title:hover { text-decoration: underline; color: var(--red); }

    .article-meta {
      font-size: 0.78rem;
      color: var(--muted);
      font-family: Arial, sans-serif;
      margin-bottom: 0.7rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.3rem;
    }

    /* ---- Source badges ---- */
    .source-badge {
      display: inline-block;
      font-size: 0.68rem;
      font-weight: 700;
      font-family: Arial, sans-serif;
      border-radius: 3px;
      padding: 0.1rem 0.4rem;
      letter-spacing: 0.3px;
    }
    .source-yahoo       { background: #6001d2; color: #fff; }
    .source-cnbc        { background: #c00;    color: #fff; }
    .source-wsj         { background: #003366; color: #fff; }
    .source-economist   { background: #e3120b; color: #fff; }
    .source-cnn         { background: #cc0000; color: #fff; }
    .source-ibd         { background: #e06000; color: #fff; }
    .source-techcrunch  { background: #0a7d3e; color: #fff; }
    .source-marketwatch { background: #004b87; color: #fff; }
    .source-reuters     { background: #ff8000; color: #fff; }
    .source-other       { background: #555;    color: #fff; }

    /* ---- Score badge ---- */
    .score-badge {
      display: inline-flex;
      align-items: center;
      background: var(--navy);
      color: #fff;
      font-size: 0.68rem;
      border-radius: 10px;
      padding: 0.1rem 0.45rem;
      font-family: Arial, sans-serif;
    }

    /* ---- Sentiment badge ---- */
    .sentiment-badge {
      display: inline-flex;
      align-items: center;
      font-size: 0.68rem;
      font-family: Arial, sans-serif;
      font-weight: 600;
      border-radius: 10px;
      padding: 0.1rem 0.45rem;
      border: 1px solid;
    }
    .sentiment-bullish { background: var(--bull-bg); color: var(--bull-fg); border-color: var(--bull-br); }
    .sentiment-bearish { background: var(--bear-bg); color: var(--bear-fg); border-color: var(--bear-br); }
    .sentiment-neutral { background: var(--neut-bg); color: var(--neut-fg); border-color: var(--neut-br); }

    /* ---- Reading time ---- */
    .reading-time {
      font-size: 0.68rem;
      font-family: Arial, sans-serif;
      color: var(--muted);
    }

    /* ---- Summary & why-it-matters ---- */
    .summary {
      font-size: 0.93rem;
      line-height: 1.65;
      margin-bottom: 0.75rem;
    }
    .why-matters {
      background: var(--why-bg);
      border-radius: 5px;
      padding: 0.6rem 0.9rem;
      font-size: 0.87rem;
      transition: background 0.25s;
    }
    .why-matters strong { color: var(--navy); font-family: Arial, sans-serif; }
    .why-matters ul { margin: 0.3rem 0 0 1.2rem; padding: 0; }
    .why-matters li { margin-bottom: 0.2rem; }

    /* ---- Shortfall notice ---- */
    .shortfall-notice {
      color: var(--red);
      font-style: italic;
      font-size: 0.9rem;
      padding: 0.5rem 0.8rem;
      border: 1px dashed var(--red);
      border-radius: 4px;
    }

    /* ---- Footer ---- */
    footer {
      margin-top: 3rem;
      padding-top: 1rem;
      border-top: 1px solid var(--border);
      font-size: 0.78rem;
      color: var(--muted);
      font-family: Arial, sans-serif;
    }
    footer p { margin-bottom: 0.3rem; }

    /* ---- Responsive ---- */
    @media (max-width: 860px) {
      .page-wrapper {
        grid-template-columns: 1fr;
        padding: 1rem 1rem 3rem;
      }
      .sidebar { display: none; }
      .category-nav { display: flex; }
      .dm-toggle { top: 0.6rem; right: 0.6rem; }
    }
  </style>
</head>
<body>

<button class="dm-toggle" id="dm-toggle" title="Toggle dark mode" aria-label="Toggle dark mode">🌙</button>

<div class="page-wrapper">

  <!-- Sticky sidebar (desktop only) -->
  <aside class="sidebar">
    <nav class="sidebar-nav" aria-label="Category navigation">
      <p class="sidebar-heading">Contents</p>
      {% for cat in categories %}
      <a href="#{{ cat.slug }}" class="sidebar-link">{{ cat.icon }} {{ cat.name }}</a>
      {% endfor %}
    </nav>
  </aside>

  <!-- Main content -->
  <main class="main-content">

    <header>
      <h1>Daily Financial Digest</h1>
      <p class="digest-meta">
        {{ date_formatted }} &nbsp;&bull;&nbsp;
        {{ total_count }} stories across {{ category_count }} categories &nbsp;&bull;&nbsp;
        Generated {{ generated_at }} UTC
      </p>
    </header>

    {% if market_data %}
    <div class="market-ticker" role="complementary" aria-label="Market snapshot">
      {% for item in market_data %}
      <div class="ticker-item ticker-{{ item.direction }}">
        <span class="ticker-name">{{ item.name }}</span>
        <span class="ticker-price">{{ item.price }}</span>
        <span class="ticker-change">{{ item.change_pct }}</span>
      </div>{% if not loop.last %}<span class="ticker-sep">|</span>{% endif %}
      {% endfor %}
    </div>
    {% endif %}

    {% if tldr %}
    <div class="tldr-section" role="note" aria-label="Today's brief">
      <p class="tldr-label">Today&rsquo;s Brief</p>
      <p class="tldr-text">{{ tldr }}</p>
    </div>
    {% endif %}

    <!-- Mobile nav pills -->
    <nav class="category-nav" aria-label="Jump to category">
      {% for cat in categories %}
      <a href="#{{ cat.slug }}">{{ cat.icon }} {{ cat.name }}</a>
      {% endfor %}
    </nav>

    {% for cat in categories %}
    <section class="category-section" id="{{ cat.slug }}">
      <h2 class="category-header">{{ cat.icon }} {{ cat.name }}</h2>
      <p class="category-desc">{{ cat.description }}</p>

      {% if cat.articles %}
        {% for article in cat.articles %}
        <div class="article-card">
          <a class="article-title" href="{{ article.url }}" target="_blank" rel="noopener noreferrer">
            {{ article.title }}
          </a>
          <div class="article-meta">
            {% set sl = article.source_label %}
            {% if "Yahoo" in sl %}<span class="source-badge source-yahoo">{{ sl }}</span>
            {% elif "CNBC" in sl %}<span class="source-badge source-cnbc">{{ sl }}</span>
            {% elif "Economist" in sl %}<span class="source-badge source-economist">{{ sl }}</span>
            {% elif "CNN" in sl %}<span class="source-badge source-cnn">{{ sl }}</span>
            {% elif "WSJ" in sl %}<span class="source-badge source-wsj">{{ sl }}</span>
            {% elif "Investor" in sl %}<span class="source-badge source-ibd">{{ sl }}</span>
            {% elif "TechCrunch" in sl %}<span class="source-badge source-techcrunch">{{ sl }}</span>
            {% elif "MarketWatch" in sl %}<span class="source-badge source-marketwatch">{{ sl }}</span>
            {% elif "Reuters" in sl %}<span class="source-badge source-reuters">{{ sl }}</span>
            {% else %}<span class="source-badge source-other">{{ sl }}</span>{% endif %}
            <span>{{ article.publish_time_human }}</span>
            {% if article.author %}<span>&bull; {{ article.author }}</span>{% endif %}
            <span class="score-badge">Score&nbsp;{{ article.total_score }}</span>
            {% if article.sentiment == "Bullish" %}
            <span class="sentiment-badge sentiment-bullish">&#x1F7E2; Bullish</span>
            {% elif article.sentiment == "Bearish" %}
            <span class="sentiment-badge sentiment-bearish">&#x1F534; Bearish</span>
            {% else %}
            <span class="sentiment-badge sentiment-neutral">&#x26AA; Neutral</span>
            {% endif %}
            <span class="reading-time">{{ article.reading_time }}&nbsp;min&nbsp;read</span>
          </div>
          <p class="summary">{{ article.summary }}</p>
          <div class="why-matters">
            <strong>Why it matters:</strong>
            <ul>
              {% for bullet in article.why_it_matters %}
              <li>{{ bullet }}</li>
              {% endfor %}
            </ul>
          </div>
        </div>
        {% endfor %}
      {% else %}
        <div class="shortfall-notice">
          No articles found for this category today. Check logs for SHORTFALL details.
        </div>
      {% endif %}
    </section>
    {% endfor %}

    <footer>
      <p>Generated {{ generated_at }} UTC &bull; Sources: Yahoo Finance &bull; CNBC &bull; CNN &bull; The Economist &bull; TechCrunch &bull; MarketWatch &bull; Reuters</p>
      <p>This digest summarises publicly available headlines and lead text. No full article text is reproduced.</p>
    </footer>

  </main>
</div>

<script>
(function () {
  'use strict';
  var btn = document.getElementById('dm-toggle');

  function applyDark(on) {
    document.body.classList.toggle('dark-mode', on);
    btn.textContent = on ? '☀️' : '🌙';
  }

  // Restore persisted preference
  try { applyDark(localStorage.getItem('dm') === '1'); } catch (e) {}

  btn.addEventListener('click', function () {
    var on = document.body.classList.toggle('dark-mode');
    btn.textContent = on ? '☀️' : '🌙';
    try { localStorage.setItem('dm', on ? '1' : '0'); } catch (e) {}
  });

  // Lazy-reveal article cards via IntersectionObserver
  if ('IntersectionObserver' in window) {
    var cardIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add('visible');
          cardIO.unobserve(e.target);
        }
      });
    }, { rootMargin: '0px 0px -40px 0px', threshold: 0.05 });

    document.querySelectorAll('.article-card').forEach(function (c) {
      cardIO.observe(c);
    });
  } else {
    // Fallback: reveal all cards immediately for older browsers
    document.querySelectorAll('.article-card').forEach(function (c) {
      c.classList.add('visible');
    });
  }

  // Highlight active sidebar link as user scrolls
  var sections = document.querySelectorAll('.category-section');
  var sideLinks = document.querySelectorAll('.sidebar-link');
  if (sections.length && sideLinks.length && 'IntersectionObserver' in window) {
    var secIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          sideLinks.forEach(function (l) { l.classList.remove('active'); });
          var active = document.querySelector('.sidebar-link[href="#' + e.target.id + '"]');
          if (active) active.classList.add('active');
        }
      });
    }, { threshold: 0.25 });
    sections.forEach(function (s) { secIO.observe(s); });
  }
})();
</script>

</body>
</html>
"""

# ------------------------------------------------------------------ #
# Template environment                                                  #
# ------------------------------------------------------------------ #

_JINJA_ENV = Environment(loader=BaseLoader(), autoescape=True)
_HTML_TMPL = _JINJA_ENV.from_string(_HTML_TEMPLATE)

# ------------------------------------------------------------------ #
# Context builder helpers                                              #
# ------------------------------------------------------------------ #

_DEFAULT_ICONS = {
    "Global": "🌍",
    "Market": "📈",
    "Stock": "🏢",
    "Tech": "💻",
}


def _build_context(
    articles_by_category: dict[str, list[Article]],
    category_defs: dict,
    config: dict,
    date_str: str,
    generated_at: str,
    tldr: str = "",
    market_data: list | None = None,
) -> dict:
    icons = (
        config.get("settings", {})
              .get("category_icons", _DEFAULT_ICONS)
    )
    categories = []
    total_count = 0

    for cat_name, articles in articles_by_category.items():
        cat_def = category_defs.get(cat_name, {})
        serialised = [a.to_dict() for a in articles]
        categories.append({
            "name": cat_name,
            "slug": cat_name.lower(),
            "icon": icons.get(cat_name, ""),
            "description": cat_def.get("description", ""),
            "articles": serialised,
        })
        total_count += len(articles)

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_formatted = dt.strftime("%A, %B %-d, %Y")
    except ValueError:
        date_formatted = date_str

    return {
        "date_str": date_str,
        "date_formatted": date_formatted,
        "generated_at": generated_at,
        "total_count": total_count,
        "category_count": len(categories),
        "categories": categories,
        "tldr": tldr,
        "market_data": market_data or [],
    }


# ------------------------------------------------------------------ #
# HTML renderer                                                         #
# ------------------------------------------------------------------ #

def render_html(
    articles_by_category: dict[str, list[Article]],
    category_defs: dict,
    config: dict,
    output_dir: Path,
    date_str: str | None = None,
    tldr: str = "",
    market_data: list | None = None,
) -> Path:
    """
    Render and write the HTML digest.
    Returns the path to the written file.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    context = _build_context(
        articles_by_category, category_defs, config, date_str, generated_at,
        tldr=tldr, market_data=market_data,
    )
    html_content = _HTML_TMPL.render(**context)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"daily_digest_{date_str}.html"
    out_path.write_text(html_content, encoding="utf-8")
    logger.info("HTML output written: %s", out_path)
    return out_path


# ------------------------------------------------------------------ #
# Markdown renderer                                                     #
# ------------------------------------------------------------------ #

def render_markdown(
    articles_by_category: dict[str, list[Article]],
    category_defs: dict,
    config: dict,
    output_dir: Path,
    date_str: str | None = None,
) -> Path:
    """
    Render and write the Markdown digest.
    Returns the path to the written file.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    icons = (
        config.get("settings", {})
              .get("category_icons", _DEFAULT_ICONS)
    )

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_formatted = dt.strftime("%A, %B %-d, %Y")
    except ValueError:
        date_formatted = date_str

    total = sum(len(v) for v in articles_by_category.values())
    lines: list[str] = []

    lines.append(f"# Daily Financial Digest — {date_formatted}")
    lines.append(f"*{total} stories &nbsp;|&nbsp; Generated {generated_at} UTC*")
    lines.append("")
    lines.append("> Sources: Yahoo Finance · CNBC · CNN · The Economist · Wall Street Journal. Summaries of public headlines only.")
    lines.append("")

    for cat_name, articles in articles_by_category.items():
        icon = icons.get(cat_name, "")
        cat_def = category_defs.get(cat_name, {})
        description = cat_def.get("description", "")

        lines.append("---")
        lines.append("")
        lines.append(f"## {icon} {cat_name}")
        lines.append("")
        if description:
            lines.append(f"*{description}*")
            lines.append("")

        if not articles:
            lines.append(
                "> **SHORTFALL**: No articles found for this category today. "
                "Check pipeline logs for details."
            )
            lines.append("")
            continue

        for i, article in enumerate(articles, 1):
            d = article.to_dict()
            pub_human = d.get("publish_time_human", "")
            author = f" | {d['author']}" if d.get("author") else ""
            score = d.get("total_score", 0.0)
            sentiment = d.get("sentiment", "Neutral")

            lines.append(f"### {i}. [{d['title']}]({d['url']})")
            lines.append(f"**{d['source_label']} | {pub_human}{author} | Score: {score} | {sentiment}**")
            lines.append("")
            lines.append(d.get("summary", ""))
            lines.append("")

            bullets = d.get("why_it_matters", [])
            if bullets:
                lines.append("**Why it matters:**")
                for bullet in bullets:
                    lines.append(f"- {bullet}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Daily Financial Digest · {generated_at} UTC*")

    md_content = "\n".join(lines)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"daily_digest_{date_str}.md"
    out_path.write_text(md_content, encoding="utf-8")
    logger.info("Markdown output written: %s", out_path)
    return out_path

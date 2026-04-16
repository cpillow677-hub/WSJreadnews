"""
Renderer — produces HTML and Markdown output files from the selected and
summarised articles.

Public interface:
    render_html(articles_by_category, category_defs, config, output_dir) -> Path
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
    :root {
      --navy:   #003366;
      --red:    #b30000;
      --bg:     #f5f5f0;
      --card:   #ffffff;
      --text:   #1a1a1a;
      --muted:  #666666;
      --border: #dddddd;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: Georgia, 'Times New Roman', serif;
      background: var(--bg);
      color: var(--text);
      max-width: 900px;
      margin: 0 auto;
      padding: 2rem 1.5rem 4rem;
      line-height: 1.6;
    }
    /* ---- Header ---- */
    header {
      border-bottom: 4px double var(--red);
      padding-bottom: 1rem;
      margin-bottom: 2.5rem;
    }
    header h1 {
      font-size: 2rem;
      color: var(--navy);
      letter-spacing: 0.02em;
    }
    .digest-meta {
      font-size: 0.88rem;
      color: var(--muted);
      margin-top: 0.4rem;
    }
    /* ---- Nav pills ---- */
    .category-nav {
      display: flex;
      gap: 0.6rem;
      flex-wrap: wrap;
      margin-bottom: 2.5rem;
    }
    .category-nav a {
      background: var(--navy);
      color: #fff;
      text-decoration: none;
      padding: 0.3rem 0.8rem;
      border-radius: 20px;
      font-size: 0.82rem;
      font-family: Arial, sans-serif;
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
    /* ---- Article card ---- */
    .article-card {
      background: var(--card);
      border-left: 4px solid var(--red);
      border-radius: 0 6px 6px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.07);
      padding: 1.1rem 1.4rem;
      margin-bottom: 1.3rem;
    }
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
    }
    .source-badge {
      display: inline-block;
      font-size: 0.68rem;
      font-weight: 700;
      font-family: Arial, sans-serif;
      border-radius: 3px;
      padding: 0.1rem 0.4rem;
      margin-right: 0.4rem;
      vertical-align: middle;
      letter-spacing: 0.3px;
    }
    .source-yahoo      { background: #6001d2; color: #fff; }
    .source-cnbc       { background: #c00;    color: #fff; }
    .source-wsj        { background: #003366; color: #fff; }
    .source-economist  { background: #e3120b; color: #fff; }
    .source-cnn        { background: #cc0000; color: #fff; }
    .source-ibd        { background: #e06000; color: #fff; }
    .source-other      { background: #555;    color: #fff; }
    .score-badge {
      display: inline-block;
      background: var(--navy);
      color: #fff;
      font-size: 0.7rem;
      border-radius: 10px;
      padding: 0.1rem 0.45rem;
      margin-left: 0.4rem;
      vertical-align: middle;
      font-family: Arial, sans-serif;
    }
    .summary {
      font-size: 0.93rem;
      line-height: 1.65;
      margin-bottom: 0.75rem;
    }
    .why-matters {
      background: #f0f4f8;
      border-radius: 5px;
      padding: 0.6rem 0.9rem;
      font-size: 0.87rem;
    }
    .why-matters strong {
      color: var(--navy);
      font-family: Arial, sans-serif;
    }
    .why-matters ul {
      margin: 0.3rem 0 0 1.2rem;
      padding: 0;
    }
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
  </style>
</head>
<body>

<header>
  <h1>Daily Financial Digest</h1>
  <p class="digest-meta">
    {{ date_formatted }} &nbsp;&bull;&nbsp;
    {{ total_count }} stories across {{ category_count }} categories &nbsp;&bull;&nbsp;
    Generated {{ generated_at }} UTC
  </p>
</header>

<nav class="category-nav">
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
        {% else %}<span class="source-badge source-other">{{ sl }}</span>{% endif %}
        {{ article.publish_time_human }}
        {% if article.author %}&bull; {{ article.author }}{% endif %}
        <span class="score-badge">Score&nbsp;{{ article.total_score }}</span>
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
  <p>Generated {{ generated_at }} UTC &bull; Sources: Yahoo Finance &bull; CNBC &bull; CNN &bull; The Economist &bull; Wall Street Journal</p>
  <p>This digest summarises publicly available headlines and lead text. No full article text is reproduced.</p>
</footer>

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
) -> Path:
    """
    Render and write the HTML digest.
    Returns the path to the written file.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    context = _build_context(
        articles_by_category, category_defs, config, date_str, generated_at
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

            lines.append(f"### {i}. [{d['title']}]({d['url']})")
            lines.append(f"**{d['source_label']} | {pub_human}{author} | Score: {score}**")
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

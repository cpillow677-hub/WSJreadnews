#!/usr/bin/env python3
"""
build_pages.py — generates the GitHub Pages static site index.

Scans docs/reports/ for daily digest HTML files (named YYYY-MM-DD.html),
then writes docs/index.html listing all reports in reverse-chronological order.

Usage:
    python scripts/build_pages.py
    python scripts/build_pages.py --docs-dir docs/
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ #
# Index page HTML template                                             #
# ------------------------------------------------------------------ #

_INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WSJ Daily Digest — Archive</title>
  <style>
    :root {{
      --navy:   #003366;
      --red:    #b30000;
      --bg:     #f5f5f0;
      --card:   #ffffff;
      --text:   #1a1a1a;
      --muted:  #666666;
      --border: #dddddd;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: Georgia, 'Times New Roman', serif;
      background: var(--bg);
      color: var(--text);
      max-width: 860px;
      margin: 0 auto;
      padding: 2rem 1.5rem 4rem;
      line-height: 1.6;
    }}
    /* ---- Header ---- */
    .site-header {{
      border-bottom: 3px double var(--navy);
      padding-bottom: 1.2rem;
      margin-bottom: 2rem;
      text-align: center;
    }}
    .site-header h1 {{
      font-size: 2.4rem;
      color: var(--navy);
      letter-spacing: -0.5px;
    }}
    .site-header .tagline {{
      color: var(--muted);
      font-style: italic;
      margin-top: 0.3rem;
      font-size: 1rem;
    }}
    .site-header .updated {{
      font-size: 0.8rem;
      color: var(--muted);
      margin-top: 0.6rem;
      font-family: Arial, sans-serif;
    }}
    /* ---- Section title ---- */
    .section-title {{
      font-family: Arial, Helvetica, sans-serif;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      padding-bottom: 0.4rem;
      margin-bottom: 1.2rem;
    }}
    /* ---- Featured (latest) ---- */
    .featured {{
      background: var(--card);
      border: 2px solid var(--navy);
      border-radius: 4px;
      padding: 1.5rem 1.8rem;
      margin-bottom: 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      text-decoration: none;
      color: inherit;
      transition: box-shadow 0.15s ease;
    }}
    .featured:hover {{ box-shadow: 0 4px 12px rgba(0,51,102,0.15); }}
    .featured .label {{
      font-family: Arial, sans-serif;
      font-size: 0.65rem;
      font-weight: 700;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      color: var(--red);
      margin-bottom: 0.4rem;
    }}
    .featured .date {{
      font-size: 1.6rem;
      font-weight: bold;
      color: var(--navy);
    }}
    .featured .weekday {{
      font-size: 0.95rem;
      color: var(--muted);
      margin-top: 0.2rem;
    }}
    .featured .arrow {{
      font-size: 1.8rem;
      color: var(--navy);
      flex-shrink: 0;
    }}
    /* ---- Archive grid ---- */
    .archive-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 0.8rem;
      margin-bottom: 2rem;
    }}
    .archive-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1rem 1.2rem;
      text-decoration: none;
      color: inherit;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}
    .archive-card:hover {{
      border-color: var(--navy);
      box-shadow: 0 2px 8px rgba(0,51,102,0.1);
    }}
    .archive-card .card-date {{
      font-size: 1rem;
      font-weight: bold;
      color: var(--navy);
    }}
    .archive-card .card-weekday {{
      font-size: 0.8rem;
      color: var(--muted);
      font-family: Arial, sans-serif;
      margin-top: 0.2rem;
    }}
    /* ---- Footer ---- */
    .site-footer {{
      border-top: 1px solid var(--border);
      padding-top: 1rem;
      text-align: center;
      font-size: 0.78rem;
      color: var(--muted);
      font-family: Arial, sans-serif;
    }}
    /* ---- Empty state ---- */
    .empty {{
      text-align: center;
      padding: 3rem;
      color: var(--muted);
      font-style: italic;
    }}
  </style>
</head>
<body>

  <header class="site-header">
    <h1>WSJ Daily Digest</h1>
    <p class="tagline">Curated Wall Street Journal highlights — Global · Market · Stock · Tech</p>
    <p class="updated">Updated: {updated}</p>
  </header>

  {featured_section}

  {archive_section}

  <footer class="site-footer">
    Generated automatically each day at 06:00 UTC &nbsp;·&nbsp;
    Powered by <a href="https://github.com/{repo}" style="color:var(--navy);">WSJreadnews</a>
  </footer>

</body>
</html>
"""

_FEATURED_SECTION = """\
  <p class="section-title">Latest Report</p>
  <a class="featured" href="reports/{filename}">
    <div>
      <div class="label">&#9679; Latest</div>
      <div class="date">{date_display}</div>
      <div class="weekday">{weekday}</div>
    </div>
    <div class="arrow">&#8594;</div>
  </a>
"""

_ARCHIVE_SECTION_OPEN = """\
  <p class="section-title">Archive ({count} reports)</p>
  <div class="archive-grid">
"""

_ARCHIVE_CARD = """\
    <a class="archive-card" href="reports/{filename}">
      <div class="card-date">{date_display}</div>
      <div class="card-weekday">{weekday}</div>
    </a>
"""

_ARCHIVE_SECTION_CLOSE = "  </div>\n"


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")


def _collect_reports(reports_dir: Path) -> list[tuple[datetime, Path]]:
    """Return (date, file_path) pairs sorted newest-first."""
    entries: list[tuple[datetime, Path]] = []
    for f in reports_dir.glob("*.html"):
        m = _DATE_RE.match(f.name)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d")
                entries.append((dt, f))
            except ValueError:
                pass
    entries.sort(key=lambda t: t[0], reverse=True)
    return entries


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%B %-d, %Y")


def _fmt_weekday(dt: datetime) -> str:
    return dt.strftime("%A")


# ------------------------------------------------------------------ #
# Builder                                                              #
# ------------------------------------------------------------------ #

def build_index(docs_dir: Path, repo: str = "cpillow677-hub/WSJreadnews") -> Path:
    """Generate docs/index.html from all reports in docs/reports/."""
    reports_dir = docs_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    entries = _collect_reports(reports_dir)
    updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not entries:
        featured_section = '  <div class="empty">No reports yet — the first digest will appear after the next scheduled run.</div>\n'
        archive_section = ""
    else:
        # Featured = newest
        latest_dt, latest_path = entries[0]
        featured_section = _FEATURED_SECTION.format(
            filename=latest_path.name,
            date_display=_fmt_date(latest_dt),
            weekday=_fmt_weekday(latest_dt),
        )

        # Archive = all reports (including newest, for completeness)
        archive_section = _ARCHIVE_SECTION_OPEN.format(count=len(entries))
        for dt, fp in entries:
            archive_section += _ARCHIVE_CARD.format(
                filename=fp.name,
                date_display=_fmt_date(dt),
                weekday=_fmt_weekday(dt),
            )
        archive_section += _ARCHIVE_SECTION_CLOSE

    html = _INDEX_TEMPLATE.format(
        updated=updated,
        featured_section=featured_section,
        archive_section=archive_section,
        repo=repo,
    )

    index_path = docs_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    print(f"[build_pages] index.html written: {index_path} ({len(entries)} reports)")
    return index_path


# ------------------------------------------------------------------ #
# CLI                                                                  #
# ------------------------------------------------------------------ #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build GitHub Pages index for WSJ Digest.")
    p.add_argument(
        "--docs-dir",
        type=Path,
        default=Path("docs"),
        help="Path to the docs/ directory (default: docs/)",
    )
    p.add_argument(
        "--repo",
        type=str,
        default="cpillow677-hub/WSJreadnews",
        help="GitHub repo slug for footer link (default: cpillow677-hub/WSJreadnews)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    build_index(args.docs_dir, args.repo)

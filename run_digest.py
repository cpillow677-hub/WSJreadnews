#!/usr/bin/env python3
"""
run_digest.py — CLI entrypoint for the WSJ Daily Digest pipeline.

Usage:
    python run_digest.py [OPTIONS]

Options:
    --config PATH         Path to categories.yaml  (default: config/categories.yaml)
    --output-dir PATH     Output directory          (default: output/)
    --date YYYY-MM-DD     Override date string      (default: today UTC)
    --dry-run             Run pipeline but skip writing output files
    --no-scrape           RSS-only mode (skip section scraping)
    --full-text           Fetch full article body for selected articles (requires WSJ auth)
    --log-level LEVEL     DEBUG|INFO|WARNING|ERROR  (default: INFO)
    --max-age-hours N     Override max_age_hours from config

Exit codes:
    0 — success (even with shortfalls)
    1 — fatal error (missing config, unrecoverable exception)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ------------------------------------------------------------------ #
# Bootstrap: load .env before anything else                            #
# ------------------------------------------------------------------ #
load_dotenv()

# Import pipeline modules
from wsj_digest.fetcher import _fetch_all_articles_with_session, enrich_with_full_text
from wsj_digest.scorer import score_articles
from wsj_digest.selector import select_top_articles
from wsj_digest.summarizer import summarize_all
from wsj_digest.renderer import render_html, render_markdown

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Config loading and validation                                         #
# ------------------------------------------------------------------ #

REQUIRED_TOP_KEYS = ["categories", "settings"]
REQUIRED_CATEGORY_KEYS = ["description", "keywords", "target_count"]


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            f"Expected at {config_path.resolve()}"
        )
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    for key in REQUIRED_TOP_KEYS:
        if key not in config:
            raise ValueError(f"config.yaml is missing required top-level key: '{key}'")
    for cat_name, cat_def in config.get("categories", {}).items():
        for req in REQUIRED_CATEGORY_KEYS:
            if req not in cat_def:
                raise ValueError(
                    f"Category '{cat_name}' in config.yaml is missing key: '{req}'"
                )


# ------------------------------------------------------------------ #
# Logging setup                                                         #
# ------------------------------------------------------------------ #

def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ------------------------------------------------------------------ #
# Argument parsing                                                      #
# ------------------------------------------------------------------ #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_digest",
        description="WSJ Daily Digest — fetch, score, summarise, and render top stories.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/categories.yaml"),
        metavar="PATH",
        help="Path to categories.yaml (default: config/categories.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output directory (default: value from config, usually 'output/')",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Override date string for output filenames (default: today UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run the full pipeline but do not write output files.",
    )
    parser.add_argument(
        "--no-scrape",
        action="store_true",
        default=False,
        help="Disable section scraping; use RSS feeds only.",
    )
    parser.add_argument(
        "--full-text",
        action="store_true",
        default=False,
        dest="full_text",
        help=(
            "Fetch full article body for selected articles before summarising. "
            "Uses Playwright when use_playwright_fulltext=true or USE_PLAYWRIGHT=1. "
            "Requires WSJ authentication (WSJ_EMAIL + WSJ_PASSWORD)."
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="Logging verbosity: DEBUG|INFO|WARNING|ERROR (default: INFO)",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=None,
        metavar="N",
        help="Override max_age_hours from config (reject articles older than N hours).",
    )
    return parser.parse_args(argv)


# ------------------------------------------------------------------ #
# Main pipeline                                                         #
# ------------------------------------------------------------------ #

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)

    logger.info("=" * 60)
    logger.info("WSJ Digest Pipeline — Start")
    logger.info("=" * 60)

    # --- Load config ---
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        logging.error("Config error: %s", exc)
        return 1

    # --- Apply CLI overrides ---
    settings = config.setdefault("settings", {})
    if args.max_age_hours is not None:
        settings["max_age_hours"] = args.max_age_hours
        logger.info("Override: max_age_hours = %s", args.max_age_hours)
    if args.no_scrape:
        settings["use_section_scraper"] = False
        logger.info("Override: section scraping disabled (--no-scrape)")
    if args.full_text:
        settings["use_full_text"] = True
        logger.info("Override: full-text enrichment enabled (--full-text)")

    output_dir    = args.output_dir or Path(settings.get("output_dir", "output"))
    date_str      = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    category_defs = config["categories"]

    logger.info("Date: %s", date_str)
    logger.info("Output dir: %s", output_dir.resolve())
    logger.info("Config: %s", args.config.resolve())

    # ---------------------------------------------------------------- #
    # Step 1: Fetch                                                      #
    # ---------------------------------------------------------------- #
    logger.info("-" * 40)
    logger.info("Step 1/5: Fetching articles...")
    try:
        articles, _session = _fetch_all_articles_with_session(config)
    except Exception as exc:
        logger.error("Fetch failed: %s", exc, exc_info=True)
        return 1
    logger.info("Step 1/5 complete: %d candidates fetched", len(articles))

    if not articles:
        logger.warning(
            "No articles fetched. Check network connectivity and RSS feed availability."
        )

    # ---------------------------------------------------------------- #
    # Step 2: Score                                                      #
    # ---------------------------------------------------------------- #
    logger.info("-" * 40)
    logger.info("Step 2/5: Scoring and classifying articles...")
    try:
        articles = score_articles(articles, config)
    except Exception as exc:
        logger.error("Scoring failed: %s", exc, exc_info=True)
        return 1
    classified = sum(1 for a in articles if a.category is not None)
    logger.info(
        "Step 2/5 complete: %d articles scored, %d classified",
        len(articles),
        classified,
    )

    # ---------------------------------------------------------------- #
    # Step 3: Dedup + Select                                             #
    # ---------------------------------------------------------------- #
    logger.info("-" * 40)
    logger.info("Step 3/5: Deduplicating and selecting top articles...")
    try:
        articles_by_category = select_top_articles(articles, category_defs, config)
    except Exception as exc:
        logger.error("Selection failed: %s", exc, exc_info=True)
        return 1
    total_selected = sum(len(v) for v in articles_by_category.values())
    logger.info(
        "Step 3/5 complete: %d articles selected across %d categories",
        total_selected,
        len(articles_by_category),
    )

    # ---------------------------------------------------------------- #
    # Step 3.5: Full-text enrichment (opt-in)                           #
    # ---------------------------------------------------------------- #
    if settings.get("use_full_text", False):
        logger.info("-" * 40)
        use_pw = (
            os.environ.get("USE_PLAYWRIGHT") == "1"
            or bool(settings.get("use_playwright_fulltext", False))
        )
        mode = "Playwright" if use_pw else "requests"
        logger.info(
            "Step 3.5/5: Fetching full article text (%s) for %d articles...",
            mode, total_selected,
        )
        try:
            articles_by_category = enrich_with_full_text(
                articles_by_category, _session, config
            )
        except Exception as exc:
            logger.warning(
                "Full-text enrichment failed: %s — continuing with lead_text only", exc
            )
        logger.info("Step 3.5/5 complete")
    else:
        logger.info(
            "Full-text enrichment disabled (use_full_text=false). "
            "Use --full-text to enable."
        )

    # ---------------------------------------------------------------- #
    # Step 4: Summarise                                                  #
    # ---------------------------------------------------------------- #
    logger.info("-" * 40)
    logger.info("Step 4/5: Generating summaries...")
    try:
        articles_by_category = summarize_all(articles_by_category, category_defs)
    except Exception as exc:
        logger.error("Summarization failed: %s", exc, exc_info=True)
        return 1
    logger.info("Step 4/5 complete: summaries generated")

    # ---------------------------------------------------------------- #
    # Step 5: Render                                                     #
    # ---------------------------------------------------------------- #
    logger.info("-" * 40)
    logger.info("Step 5/5: Rendering output files...")
    if args.dry_run:
        logger.info("DRY RUN — skipping file write.")
        logger.info("Would write:")
        logger.info("  %s/daily_digest_%s.html", output_dir, date_str)
        logger.info("  %s/daily_digest_%s.md",   output_dir, date_str)
    else:
        try:
            html_path = render_html(
                articles_by_category, category_defs, config, output_dir, date_str
            )
            md_path = render_markdown(
                articles_by_category, category_defs, config, output_dir, date_str
            )
            logger.info("HTML  → %s", html_path)
            logger.info("Markdown → %s", md_path)
        except Exception as exc:
            logger.error("Rendering failed: %s", exc, exc_info=True)
            return 1
    logger.info("Step 5/5 complete")

    logger.info("=" * 60)
    logger.info("WSJ Digest Pipeline — Complete")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

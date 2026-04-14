"""
Selector — deduplication and per-category top-N article selection.

Public interface:
    select_top_articles(articles, category_defs, config) -> dict[str, list[Article]]
"""
from __future__ import annotations

import logging
from typing import Optional

from rapidfuzz import fuzz

from .models import Article

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Deduplication                                                         #
# ------------------------------------------------------------------ #

def deduplicate_by_url(articles: list[Article]) -> list[Article]:
    """
    O(n) exact-URL dedup.  Keeps the first occurrence of each URL.
    Earlier entries in the list are already sorted by score (descending),
    so the best version of each URL is kept automatically.
    """
    seen: set[str] = set()
    result: list[Article] = []
    for a in articles:
        if a.url not in seen:
            seen.add(a.url)
            result.append(a)
        else:
            a.is_duplicate = True
            a.duplicate_of_url = a.url
    return result


def deduplicate_fuzzy(
    articles: list[Article],
    threshold: int = 85,
) -> list[Article]:
    """
    O(n²) pairwise fuzzy title dedup using rapidfuzz token_sort_ratio.

    `token_sort_ratio` is used instead of plain `ratio` because WSJ
    titles for the same story often reorder words, e.g.:
        "Apple Earnings Beat: Stock Rises"
        "Stock Rises After Apple Earnings Beat"

    The lower-scored article is marked as a duplicate; the higher-scored
    one is kept.  Assumes the input is already sorted by total_score
    descending (scorer.py guarantees this).
    """
    n = len(articles)
    for i in range(n):
        if articles[i].is_duplicate:
            continue
        for j in range(i + 1, n):
            if articles[j].is_duplicate:
                continue
            ratio = fuzz.token_sort_ratio(articles[i].title, articles[j].title)
            if ratio >= threshold:
                # articles[i] has a higher (or equal) score — keep it
                articles[j].is_duplicate = True
                articles[j].duplicate_of_url = articles[i].url
                logger.debug(
                    "Fuzzy dedup (ratio=%d): '%s'  →  '%s'",
                    ratio,
                    articles[j].title[:60],
                    articles[i].title[:60],
                )
    return [a for a in articles if not a.is_duplicate]


# ------------------------------------------------------------------ #
# Selection                                                             #
# ------------------------------------------------------------------ #

def select_top_articles(
    articles: list[Article],
    category_defs: dict,
    config: dict,
) -> dict[str, list[Article]]:
    """
    Run deduplication, then select the top `target_count` articles per
    category.  Logs a WARNING (never silently skips) if the pool is
    smaller than the target.

    Returns a dict keyed by category name in the order defined in
    category_defs.
    """
    settings = config.get("settings", {})
    threshold = settings.get("dedup_threshold", 85)

    # Step 1: URL-exact dedup
    articles = deduplicate_by_url(articles)
    logger.info("After URL dedup: %d articles remain", len(articles))

    # Step 2: Fuzzy title dedup
    articles = deduplicate_fuzzy(articles, threshold=threshold)
    logger.info(
        "After fuzzy dedup (threshold=%d): %d articles remain",
        threshold,
        len(articles),
    )

    # Step 3: Per-category selection
    selected: dict[str, list[Article]] = {}
    for cat_name, cat_def in category_defs.items():
        target = int(cat_def.get("target_count", 3))
        pool = [
            a for a in articles
            if a.category == cat_name and not a.is_duplicate
        ]
        # pool is already sorted by total_score desc (from scorer)
        chosen = pool[:target]

        if len(chosen) < target:
            shortfall = target - len(chosen)
            logger.warning(
                "SHORTFALL: Category '%s' has %d/%d articles. "
                "Missing %d. Pool size was %d. "
                "Consider adjusting keywords or increasing max_age_hours.",
                cat_name,
                len(chosen),
                target,
                shortfall,
                len(pool),
            )
        else:
            logger.info(
                "Category '%s': selected %d/%d articles (pool=%d)",
                cat_name, len(chosen), target, len(pool),
            )

        selected[cat_name] = chosen

    total = sum(len(v) for v in selected.values())
    logger.info("Total selected: %d articles across %d categories", total, len(selected))
    return selected

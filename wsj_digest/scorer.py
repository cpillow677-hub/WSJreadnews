"""
Scorer — assigns importance, recency, and market-relevance scores to each
Article, then classifies it into a category via keyword matching.

Public interface:
    score_articles(articles, config) -> list[Article]   # sorted by total_score desc
    classify_article(article, category_defs) -> tuple[str, float]
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .models import Article

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Market-relevance signal terms                                        #
# ------------------------------------------------------------------ #
MARKET_SIGNALS = [
    "earnings", "revenue", "profit", "loss", "rate", "fed",
    "federal reserve", "s&p", "dow", "nasdaq", "yield", "bond",
    "oil", "gold", "dollar", "euro", "yen", "gdp", "inflation",
    "recession", "ipo", "merger", "acquisition", "dividend",
    "buyback", "guidance", "forecast", "upgrade", "downgrade",
    "basis points", "treasury", "commodity",
]


# ------------------------------------------------------------------ #
# Classification                                                       #
# ------------------------------------------------------------------ #

def classify_article(
    article: Article,
    category_defs: dict,
) -> tuple[Optional[str], float]:
    """
    Return (best_category_name, confidence_0_to_100).

    Title is weighted 3× vs lead_text because headline keywords are
    stronger classification signals.  Returns (None, 0.0) when no
    category scores above zero.
    """
    search_text = (
        (article.title + " ") * 3 + " " + article.lead_text
    ).lower()

    scores: dict[str, float] = {}
    for cat_name, cat_def in category_defs.items():
        primary_kws = cat_def.get("keywords", {}).get("primary", [])
        secondary_kws = cat_def.get("keywords", {}).get("secondary", [])
        primary_hits = sum(
            1 for kw in primary_kws if kw.lower() in search_text
        )
        secondary_hits = sum(
            1 for kw in secondary_kws if kw.lower() in search_text
        )
        scores[cat_name] = (primary_hits * 12.0) + (secondary_hits * 4.0)

    best_cat = max(scores, key=scores.get) if scores else None
    best_score = scores.get(best_cat, 0.0) if best_cat else 0.0
    confidence = min(100.0, best_score)

    if confidence == 0.0:
        return None, 0.0
    return best_cat, confidence


# ------------------------------------------------------------------ #
# Importance score                                                      #
# ------------------------------------------------------------------ #

def _compute_importance(article: Article, category_defs: dict) -> float:
    """
    Keyword-density importance score 0–100.
    Tier-1 (primary) = 10 pts each hit; tier-2 (secondary) = 4 pts each.
    Title counted 3× as a stronger signal.
    """
    search_text = (
        (article.title + " ") * 3 + " " + article.lead_text
    ).lower()

    tier1_hits = 0
    tier2_hits = 0
    for cat_def in category_defs.values():
        kws = cat_def.get("keywords", {})
        for kw in kws.get("primary", []):
            if kw.lower() in search_text:
                tier1_hits += 1
        for kw in kws.get("secondary", []):
            if kw.lower() in search_text:
                tier2_hits += 1

    raw = (tier1_hits * 10) + (tier2_hits * 4)
    return min(100.0, float(raw))


# ------------------------------------------------------------------ #
# Recency score                                                         #
# ------------------------------------------------------------------ #

def _compute_reference_time(articles: list[Article]) -> datetime:
    """
    Return the reference datetime for recency calculations.

    Normally returns datetime.now(UTC).  When the system clock is more than
    24 h ahead of the newest feed article (e.g. a dev environment with a
    future system date), returns the newest article's publish_time so that
    recency scores reflect article-relative freshness rather than being 0
    for every article.
    """
    now = datetime.now(timezone.utc)
    if not articles:
        return now

    def _pub_utc(a: Article) -> datetime:
        t = a.publish_time
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    newest_pub   = max(_pub_utc(a) for a in articles)
    clock_lead_h = (now - newest_pub).total_seconds() / 3600

    if clock_lead_h > 24:
        logger.debug(
            "Recency scorer: system clock is %.0fh ahead of newest article "
            "(%s UTC) — using article-relative reference time.",
            clock_lead_h,
            newest_pub.strftime("%Y-%m-%d %H:%M"),
        )
        return newest_pub
    return now


def _compute_recency(
    article: Article,
    max_age_hours: float = 48.0,
    reference_time: Optional[datetime] = None,
) -> float:
    """
    Piecewise-linear decay from 100 (just published) to 0 (older than max_age).

    reference_time: the "now" used for age calculation (defaults to
    datetime.now UTC).  Pass a different value to correct for system clock
    drift relative to feed publication dates.

    Breakpoints:
      ≤ 2h  → 100
      ≤ 6h  → 90 → 70   (−5/h)
      ≤ 12h → 70 → 46   (−4/h)
      ≤ 24h → 46 → 16   (−2.5/h)
      ≤ 48h → 16 → 5    (−0.45/h)
      > 48h → 0
    """
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)

    pub = article.publish_time
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age = (reference_time - pub).total_seconds() / 3600

    if age <= 2:
        return 100.0
    if age <= 6:
        return 90.0 - (age - 2) * 5.0
    if age <= 12:
        return 70.0 - (age - 6) * 4.0
    if age <= 24:
        return 46.0 - (age - 12) * 2.5
    if age <= 48:
        return max(5.0, 16.0 - (age - 24) * 0.458)
    return 0.0


# ------------------------------------------------------------------ #
# Market-relevance score                                               #
# ------------------------------------------------------------------ #

def _compute_market_relevance(article: Article) -> float:
    """
    Signal-term count × 8, capped at 100.
    """
    text = (article.title + " " + article.lead_text).lower()
    hits = sum(1 for sig in MARKET_SIGNALS if sig in text)
    return min(100.0, float(hits * 8))


# ------------------------------------------------------------------ #
# Composite score                                                       #
# ------------------------------------------------------------------ #

def _compute_total(
    importance: float,
    recency: float,
    market_rel: float,
    weights: dict,
) -> float:
    w_imp = weights.get("importance_weight", 0.45)
    w_rec = weights.get("recency_weight", 0.35)
    w_mkt = weights.get("market_relevance_weight", 0.20)
    return (importance * w_imp) + (recency * w_rec) + (market_rel * w_mkt)


# ------------------------------------------------------------------ #
# Public entry point                                                   #
# ------------------------------------------------------------------ #

def score_articles(articles: list[Article], config: dict) -> list[Article]:
    """
    Compute all scores and category classifications for each article
    in-place.  Returns the same list sorted by total_score descending.
    """
    settings = config.get("settings", {})
    weights = settings.get("scoring", {})
    max_age = settings.get("max_age_hours", 48.0)
    category_defs = config.get("categories", {})

    # Reference time for recency: corrects for system clock ahead of feed dates
    reference_time = _compute_reference_time(articles)

    for article in articles:
        try:
            article.importance_score = _compute_importance(article, category_defs)
            article.recency_score = _compute_recency(article, max_age, reference_time)
            article.market_relevance_score = _compute_market_relevance(article)
            article.total_score = _compute_total(
                article.importance_score,
                article.recency_score,
                article.market_relevance_score,
                weights,
            )
            category, cat_score = classify_article(article, category_defs)
            article.category = category
            article.category_score = cat_score
        except Exception as exc:
            logger.warning(
                "Scoring failed for article '%s': %s", article.title, exc
            )
            # Leave scores at 0.0; article stays in list but will sort to bottom

    articles.sort(key=lambda a: a.total_score, reverse=True)
    logger.debug(
        "Scored %d articles. Top score: %.1f  Bottom score: %.1f",
        len(articles),
        articles[0].total_score if articles else 0,
        articles[-1].total_score if articles else 0,
    )
    return articles

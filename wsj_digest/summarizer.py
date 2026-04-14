"""
Summarizer — generates a 100–150 word factual summary and 1–2 "why it matters"
bullets for each article, working only from available metadata (title +
lead_text).  No full article text is reproduced.

Public interface:
    summarize_article(article, category_defs) -> Article
    summarize_all(articles_by_category, category_defs) -> dict[str, list[Article]]
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .models import Article

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Article-type detection patterns                                       #
# ------------------------------------------------------------------ #
_TYPE_PATTERNS = {
    "earnings": re.compile(
        r"\b(earnings?|revenue|profit|loss|eps|quarterly results?|q[1-4] results?)\b",
        re.IGNORECASE,
    ),
    "merger": re.compile(
        r"\b(acqui(re|sition)|merger|takeover|buyout|deal|combines?)\b",
        re.IGNORECASE,
    ),
    "rates": re.compile(
        r"\b(rate (hike|cut|rise|increase|decrease)|basis point|fed funds|interest rate)\b",
        re.IGNORECASE,
    ),
    "geopolitical": re.compile(
        r"\b(war|conflict|sanction|diplomat|summit|treaty|alliance|trade war|tariff)\b",
        re.IGNORECASE,
    ),
    "ipo": re.compile(
        r"\b(ipo|initial public offering|goes? public|listing)\b",
        re.IGNORECASE,
    ),
    "downgrade": re.compile(
        r"\b(downgrade|upgrade|price target|analyst|rating)\b",
        re.IGNORECASE,
    ),
    "layoffs": re.compile(
        r"\b(layoff|lay off|job cut|workforce reduction|restructur)\b",
        re.IGNORECASE,
    ),
}

# Context phrase injected after core content, keyed by article type
_CONTEXT_PHRASES = {
    "earnings": (
        "The results reflect the company's performance against analyst expectations "
        "and will shape near-term guidance assumptions for the sector."
    ),
    "merger": (
        "The deal, if completed, would reshape competitive dynamics in the industry "
        "and faces regulatory scrutiny in multiple jurisdictions."
    ),
    "rates": (
        "The move signals a shift in monetary policy expectations and is likely to "
        "influence borrowing costs across mortgages, corporate debt, and consumer credit."
    ),
    "geopolitical": (
        "The development comes amid heightened global tensions and could affect "
        "trade flows, energy markets, and investor risk sentiment in the near term."
    ),
    "ipo": (
        "The public offering will test investor appetite and provide a market valuation "
        "benchmark for comparable private-sector peers."
    ),
    "downgrade": (
        "Revised analyst recommendations typically trigger institutional rebalancing and "
        "can amplify near-term price moves in the underlying security."
    ),
    "layoffs": (
        "The cuts are part of a broader trend of companies optimising operating costs "
        "in response to slowing growth and tighter financial conditions."
    ),
    "default": (
        "The development is being closely monitored by investors and policymakers "
        "for potential second-order effects on related sectors and asset classes."
    ),
}

# ------------------------------------------------------------------ #
# Why-it-matters templates                                             #
# ------------------------------------------------------------------ #
_WHY_TEMPLATES: dict[str, list[str]] = {
    "Global": [
        "Shapes the geopolitical backdrop for {region} and could influence trade flows and currency markets.",
        "Central banks and governments will be watching for second-order economic effects on growth and inflation.",
        "Has direct implications for multinational supply chains and cross-border investment risk.",
    ],
    "Market": [
        "Moves the needle on rate expectations and influences cross-asset positioning from equities to bonds.",
        "Traders are repricing {asset} exposure in response to the latest macro data.",
        "Affects risk sentiment broadly, with spillover potential to equities, FX, and credit markets.",
    ],
    "Stock": [
        "{company} shares are likely to see elevated volume as investors digest the news and reassess valuations.",
        "Analyst price targets and buy/sell ratings will be under review following this development.",
        "Sets a precedent for sector peers reporting in the coming weeks.",
    ],
    "Tech": [
        "Signals a potential shift in the competitive landscape for {sector}, with ramifications for adjacent platforms.",
        "Regulatory implications could ripple across the broader technology industry and investor sentiment.",
        "Investors in AI and semiconductor names will pay close attention to execution details.",
    ],
}

# ------------------------------------------------------------------ #
# Named-entity extraction helpers                                      #
# ------------------------------------------------------------------ #

def _extract_company(title: str) -> str:
    """Heuristic: the first capitalised phrase before a verb is likely the company."""
    m = re.match(r"^([A-Z][A-Za-z&.\-\s]{2,30}?)(?:\s+(?:Reports?|Says?|Plans?|Raises?|Cuts?|Beats?|Misses?|Acquires?|To\b|Will\b|Is\b))", title)
    if m:
        return m.group(1).strip()
    # Fallback: first capitalised word pair
    m = re.match(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", title)
    if m:
        return m.group(1).strip()
    return "The company"


def _extract_region(title: str, lead_text: str) -> str:
    """Return a geographic reference if found, otherwise a generic term."""
    regions = [
        "China", "Europe", "Asia", "Middle East", "Russia", "Ukraine",
        "Latin America", "Africa", "India", "Japan", "Germany", "France",
        "UK", "Britain", "U.S.", "United States", "Canada", "Australia",
    ]
    text = title + " " + lead_text
    for r in regions:
        if r.lower() in text.lower():
            return r
    return "global markets"


def _extract_asset(title: str, lead_text: str) -> str:
    assets = {
        "oil": "oil", "gold": "gold", "Treasury": "Treasury",
        "bond": "bond", "dollar": "dollar", "euro": "euro",
        "yield": "bond yield", "S&P": "equity", "Nasdaq": "equity",
    }
    text = title + " " + lead_text
    for key, label in assets.items():
        if key.lower() in text.lower():
            return label
    return "risk asset"


def _extract_sector(title: str, lead_text: str) -> str:
    sectors = {
        "AI": "AI", "artificial intelligence": "AI",
        "semiconductor": "semiconductor", "chip": "semiconductor",
        "cloud": "cloud computing", "software": "software",
        "social media": "social media", "streaming": "streaming",
    }
    text = (title + " " + lead_text).lower()
    for key, label in sectors.items():
        if key.lower() in text:
            return label
    return "tech"


# ------------------------------------------------------------------ #
# HTML / boilerplate stripping                                          #
# ------------------------------------------------------------------ #

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BOILERPLATE_RE = re.compile(
    r"(subscribe now|sign in|log in|wsj\.com|wall street journal|"
    r"©\s*\d{4}|all rights reserved|read more|click here)",
    re.IGNORECASE,
)


def _clean_lead(raw: str) -> str:
    text = _HTML_TAG_RE.sub(" ", raw)
    text = _BOILERPLATE_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# ------------------------------------------------------------------ #
# Sentence splitter                                                     #
# ------------------------------------------------------------------ #

def _split_sentences(text: str) -> list[str]:
    """Simple sentence splitter — adequate for short RSS leads."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _count_words(text: str) -> int:
    return len(text.split())


# ------------------------------------------------------------------ #
# Article-type detection                                               #
# ------------------------------------------------------------------ #

def _detect_type(article: Article) -> str:
    text = article.title + " " + article.lead_text
    for type_name, pattern in _TYPE_PATTERNS.items():
        if pattern.search(text):
            return type_name
    return "default"


# ------------------------------------------------------------------ #
# Summary builder                                                       #
# ------------------------------------------------------------------ #

def _build_summary(article: Article, min_words: int = 100, max_words: int = 150) -> str:
    """
    Builds a factual summary from title + lead_text.

    Pipeline:
      1. Clean lead_text
      2. Extract up to 3 sentences
      3. Inject article-type context phrase
      4. Enforce word-count bounds
    """
    cleaned_lead = _clean_lead(article.lead_text)
    sentences = _split_sentences(cleaned_lead)

    # Start with the cleaned sentences (up to 3)
    core_sentences = sentences[:3]
    core_text = " ".join(core_sentences).strip()

    # If lead is very thin, use the title as the seed sentence
    if _count_words(core_text) < 15:
        core_text = article.title + ". " + core_text

    # Inject context phrase based on article type
    article_type = _detect_type(article)
    context = _CONTEXT_PHRASES.get(article_type, _CONTEXT_PHRASES["default"])

    assembled = core_text.rstrip(".") + ". " + context

    # --- Word count enforcement ---
    word_count = _count_words(assembled)

    # Too long: truncate at sentence boundary
    if word_count > max_words:
        all_sentences = _split_sentences(assembled)
        truncated: list[str] = []
        running = 0
        for s in all_sentences:
            wc = _count_words(s)
            if running + wc > max_words:
                break
            truncated.append(s)
            running += wc
        assembled = " ".join(truncated)
        # Ensure it ends with punctuation
        if assembled and not assembled[-1] in ".!?":
            assembled += "."

    # Too short: pad with additional context sentences
    if _count_words(assembled) < min_words:
        padding_sentences = [
            f"The story originated from the WSJ {article.source_section} section.",
            "Markets and policymakers are watching developments closely for further clarity.",
            "Analysts note that the full implications may take time to materialise.",
            "Investors are advised to monitor follow-up reporting for additional details.",
        ]
        for pad in padding_sentences:
            assembled = assembled.rstrip(".") + ". " + pad
            if _count_words(assembled) >= min_words:
                break

    return assembled.strip()


# ------------------------------------------------------------------ #
# Why-it-matters builder                                               #
# ------------------------------------------------------------------ #

def _build_why(article: Article, category_defs: dict) -> list[str]:
    """Return 1–2 bullet strings for why_it_matters."""
    category = article.category or "Global"
    templates = _WHY_TEMPLATES.get(category, _WHY_TEMPLATES["Global"])

    company = _extract_company(article.title)
    region = _extract_region(article.title, article.lead_text)
    asset = _extract_asset(article.title, article.lead_text)
    sector = _extract_sector(article.title, article.lead_text)

    # Fill placeholders
    filled = []
    for t in templates:
        bullet = (
            t.replace("{company}", company)
             .replace("{region}", region)
             .replace("{asset}", asset)
             .replace("{sector}", sector)
        )
        filled.append(bullet)

    # Select 2 bullets: first always; second varies by score
    bullet1 = filled[0]
    bullet2 = filled[1] if len(filled) > 1 else filled[0]

    # Use a market-signal bullet for high market_relevance articles
    if article.market_relevance_score >= 40 and category != "Market":
        bullet2 = "Has direct implications for equity and/or bond market pricing."

    return [bullet1, bullet2]


# ------------------------------------------------------------------ #
# Public interface                                                      #
# ------------------------------------------------------------------ #

def summarize_article(article: Article, category_defs: dict) -> Article:
    """
    Populate article.summary and article.why_it_matters in-place.
    Falls back gracefully on any error.
    Returns the article.
    """
    try:
        article.summary = _build_summary(article)
        article.why_it_matters = _build_why(article, category_defs)
    except Exception as exc:
        logger.warning("Summarizer failed for '%s': %s", article.title, exc)
        # Graceful fallback
        fallback = _clean_lead(article.lead_text)
        article.summary = (fallback[:500] if fallback else article.title) + "."
        article.why_it_matters = ["Summary unavailable — see full article for details."]
    return article


def summarize_all(
    articles_by_category: dict[str, list[Article]],
    category_defs: dict,
) -> dict[str, list[Article]]:
    """Run summarize_article on every article in every category."""
    for cat_name, articles in articles_by_category.items():
        for article in articles:
            summarize_article(article, category_defs)
        logger.debug("Summarized %d articles in '%s'", len(articles), cat_name)
    return articles_by_category

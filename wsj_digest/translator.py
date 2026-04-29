"""
translator.py — post-process article summaries into the user's chosen language.

Uses deep-translator (GoogleTranslator) for free translation.
Only active when language != 'en'.

Public interface:
    translate_articles(articles_by_category, language) -> dict
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Article

logger = logging.getLogger(__name__)

SUPPORTED = {"en", "zh-TW", "ja", "ko", "es"}

# Map our language codes to deep-translator target codes
_LANG_MAP = {
    "zh-TW": "zh-TW",
    "ja":    "ja",
    "ko":    "ko",
    "es":    "es",
}


def _get_translator(language: str):
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target=_LANG_MAP[language])
    except ImportError:
        raise RuntimeError(
            "deep-translator not installed. Run: pip install deep-translator"
        )


def _translate(text: str, translator) -> str:
    if not text or not text.strip():
        return text
    try:
        result = translator.translate(text)
        return result if result else text
    except Exception as exc:
        logger.warning("Translation failed: %s", exc)
        return text


def translate_articles(
    articles_by_category: dict[str, list],
    language: str,
    rate_sec: float = 0.3,
) -> dict[str, list]:
    """
    Translate article.summary and article.why_it_matters into `language`.
    Article titles are left in original English (RSS source language).
    Returns the same dict (mutated in place).
    """
    if language == "en" or language not in SUPPORTED:
        return articles_by_category

    logger.info("Translating summaries to '%s'...", language)
    translator = _get_translator(language)
    total = sum(len(v) for v in articles_by_category.values())
    done  = 0

    for articles in articles_by_category.values():
        for article in articles:
            # Translate summary
            if article.summary:
                article.summary = _translate(article.summary, translator)
                time.sleep(rate_sec)

            # Translate why_it_matters bullets
            if article.why_it_matters:
                translated_bullets = []
                for bullet in article.why_it_matters:
                    translated_bullets.append(_translate(bullet, translator))
                    time.sleep(rate_sec)
                article.why_it_matters = translated_bullets

            done += 1
            logger.debug("Translated %d/%d articles", done, total)

    logger.info("Translation complete: %d articles → '%s'", total, language)
    return articles_by_category

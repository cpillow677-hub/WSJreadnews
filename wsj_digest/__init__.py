"""
wsj_digest — WSJ Daily Digest pipeline package.

Modules:
    models      Article dataclass
    fetcher     RSS + section scraping
    scorer      Importance / recency / market-relevance scoring
    selector    Deduplication and per-category selection
    summarizer  Summary and why-it-matters generation
    renderer    HTML and Markdown output
"""

__version__ = "0.1.0"
__all__ = [
    "models",
    "fetcher",
    "scorer",
    "selector",
    "summarizer",
    "renderer",
]

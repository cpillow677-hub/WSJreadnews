"""
Fetcher — collects Article candidates from WSJ RSS feeds and (optionally)
authenticated section pages.

Public interface:
    fetch_all_articles(config: dict) -> list[Article]

Architecture:
    RSSFetcher      — requests + BeautifulSoup XML parsing of DJ/WSJ RSS feeds
    SectionScraper  — requests + BeautifulSoup HTML scraping of wsj.com sections
    PlaywrightFetcher (stub) — enabled via USE_PLAYWRIGHT=1 env var
"""
from __future__ import annotations

import email.utils
import logging
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .models import Article

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Playwright opt-in guard                                              #
# ------------------------------------------------------------------ #
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ------------------------------------------------------------------ #
# WSJ login endpoints                                                  #
# ------------------------------------------------------------------ #
WSJ_HOME_URL = "https://www.wsj.com"
WSJ_LOGIN_URL = "https://id.wsj.com/auth/submitlogin.json"

# Section URLs for HTML scraping fallback
SECTION_URLS = {
    "world":    "https://www.wsj.com/world",
    "markets":  "https://www.wsj.com/markets",
    "tech":     "https://www.wsj.com/tech",
    "business": "https://www.wsj.com/business",
}

# Known public RSS feeds (merged with config yaml feeds at runtime)
DEFAULT_RSS_FEEDS = [
    {"url": "https://feeds.a.dj.com/rss/WSJRSS.xml",        "section": "top"},
    {"url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",  "section": "world"},
    {"url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml","section": "markets"},
    {"url": "https://feeds.a.dj.com/rss/RSSWSJD.xml",       "section": "business"},
    {"url": "https://feeds.a.dj.com/rss/RSSOpinion.xml",    "section": "opinion"},
]


# ------------------------------------------------------------------ #
# Base class                                                           #
# ------------------------------------------------------------------ #
class BaseFetcher(ABC):
    def __init__(self, session: requests.Session, config: dict):
        self.session = session
        self.config = config

    @abstractmethod
    def fetch(self) -> list[Article]:
        ...


# ------------------------------------------------------------------ #
# RSS Fetcher                                                          #
# ------------------------------------------------------------------ #
class RSSFetcher(BaseFetcher):
    """Fetches articles from WSJ RSS feeds."""

    def __init__(self, session: requests.Session, config: dict):
        super().__init__(session, config)
        # Merge default feeds with any feeds declared in category YAML blocks
        self.feeds = list(DEFAULT_RSS_FEEDS)
        for cat_name, cat_def in config.get("categories", {}).items():
            for feed in cat_def.get("rss_feeds", []):
                if not any(f["url"] == feed["url"] for f in self.feeds):
                    self.feeds.append({
                        "url": feed["url"],
                        "section": cat_name.lower(),
                        "name": feed.get("name", feed["url"]),
                    })

    def fetch(self) -> list[Article]:
        articles: list[Article] = []
        for feed_meta in self.feeds:
            try:
                new = self._fetch_one_feed(feed_meta)
                logger.debug("Feed '%s': fetched %d items", feed_meta["url"], len(new))
                articles.extend(new)
            except Exception as exc:
                logger.warning("Feed '%s' failed: %s", feed_meta["url"], exc)
        return articles

    def _fetch_one_feed(self, feed_meta: dict) -> list[Article]:
        resp = self.session.get(feed_meta["url"], timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")
        articles = []
        for item in items:
            try:
                article = self._parse_rss_item(item, feed_meta)
                if article:
                    articles.append(article)
            except Exception as exc:
                title_tag = item.find("title")
                title_str = title_tag.get_text(strip=True) if title_tag else "?"
                logger.warning("Failed to parse RSS item '%s': %s", title_str, exc)
        return articles

    def _parse_rss_item(self, item, feed_meta: dict) -> Optional[Article]:
        title_tag = item.find("title")
        if not title_tag:
            return None
        title = title_tag.get_text(strip=True)
        if not title:
            return None

        # URL
        link_tag = item.find("link")
        url = ""
        if link_tag:
            url = link_tag.get_text(strip=True) or link_tag.get("href", "")
        # Some feeds put URL in <guid>
        if not url:
            guid_tag = item.find("guid")
            if guid_tag:
                candidate = guid_tag.get_text(strip=True)
                if candidate.startswith("http"):
                    url = candidate
        if not url:
            return None

        # Strip tracking query params but keep the base URL intact
        url = _clean_url(url)

        # Lead text from <description>
        lead_text = ""
        desc_tag = item.find("description")
        if desc_tag:
            raw_desc = desc_tag.get_text(separator=" ", strip=True)
            lead_text = _strip_html_tags(raw_desc).strip()

        # Author
        author = ""
        for tag_name in ("dc:creator", "author", "creator"):
            author_tag = item.find(tag_name)
            if author_tag:
                author = author_tag.get_text(strip=True)
                break

        # Publish time
        publish_time = _parse_pubdate(item)

        section = feed_meta.get("section", "general")
        feed_name = feed_meta.get("name", feed_meta["url"])

        return Article(
            title=title,
            url=url,
            source_section=section,
            source_feed=feed_name,
            publish_time=publish_time,
            lead_text=lead_text,
            author=author,
        )


# ------------------------------------------------------------------ #
# Section Scraper                                                      #
# ------------------------------------------------------------------ #
class SectionScraper(BaseFetcher):
    """
    Authenticated HTML scraper for wsj.com section pages.
    Only used when RSS pool is insufficient.
    """

    def fetch(self) -> list[Article]:
        articles: list[Article] = []
        sections = list(SECTION_URLS.items())
        for section_name, url in sections:
            try:
                new = self._scrape_section(section_name, url)
                logger.debug("Section scrape '%s': %d items", section_name, len(new))
                articles.extend(new)
            except Exception as exc:
                logger.warning("Section scrape '%s' failed: %s", section_name, exc)
        return articles

    def _scrape_section(self, section_name: str, url: str) -> list[Article]:
        resp = self.session.get(url, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        articles = []

        # WSJ section pages wrap articles in <article> tags or headline links
        article_tags = soup.find_all("article")
        if not article_tags:
            # Fallback: look for anchor tags containing article paths
            article_tags = soup.find_all("a", href=re.compile(r"/articles/"))

        for tag in article_tags:
            try:
                article = self._parse_article_tag(tag, section_name)
                if article:
                    articles.append(article)
            except Exception as exc:
                logger.debug("Section parse error (%s): %s", section_name, exc)
        return articles

    def _parse_article_tag(self, tag, section_name: str) -> Optional[Article]:
        # Try to find the headline anchor
        if tag.name == "a":
            anchor = tag
        else:
            anchor = tag.find("a", href=re.compile(r"/articles/"))
        if not anchor:
            return None

        href = anchor.get("href", "")
        if not href or "/articles/" not in href:
            return None

        url = href if href.startswith("http") else f"https://www.wsj.com{href}"
        url = _clean_url(url)

        # Title from anchor text or h2/h3 inside the block
        title = anchor.get_text(strip=True)
        if not title or len(title) < 10:
            heading = tag.find(["h2", "h3", "h4"])
            if heading:
                title = heading.get_text(strip=True)
        if not title or len(title) < 10:
            return None

        # Lead text from first <p>
        lead_text = ""
        para = tag.find("p")
        if para:
            lead_text = para.get_text(strip=True)

        # Publish time from <time datetime="...">
        publish_time = datetime.now(timezone.utc)
        time_tag = tag.find("time")
        if time_tag and time_tag.get("datetime"):
            try:
                publish_time = datetime.fromisoformat(
                    time_tag["datetime"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        return Article(
            title=title,
            url=url,
            source_section=section_name,
            source_feed=f"wsj.com/{section_name}",
            publish_time=publish_time,
            lead_text=lead_text,
        )


# ------------------------------------------------------------------ #
# Playwright Fetcher (stub)                                            #
# ------------------------------------------------------------------ #
class PlaywrightFetcher(BaseFetcher):
    """
    Browser-automation fetcher — opt-in via USE_PLAYWRIGHT=1.

    Useful when WSJ serves JavaScript-rendered content that requests cannot
    parse.  Requires:  pip install playwright && playwright install chromium

    Not implemented by default; swap in your own logic here.
    """

    def fetch(self) -> list[Article]:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
        raise NotImplementedError(
            "PlaywrightFetcher.fetch() is a stub. "
            "Implement browser-based scraping here or use RSSFetcher."
        )


# ------------------------------------------------------------------ #
# Authentication helpers                                               #
# ------------------------------------------------------------------ #

def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; WSJDigestBot/1.0; "
            "+https://github.com/user/WSJreadnews)"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    return session


def _login(session: requests.Session) -> bool:
    """
    Attempt to authenticate the session against WSJ.
    Reads WSJ_EMAIL and WSJ_PASSWORD from environment.
    Returns True on success, False on any failure.
    """
    email_addr = os.environ.get("WSJ_EMAIL", "")
    password = os.environ.get("WSJ_PASSWORD", "")
    if not email_addr or not password:
        logger.info(
            "WSJ_EMAIL / WSJ_PASSWORD not set — running in RSS-only (unauthenticated) mode."
        )
        return False

    try:
        # Step 1: visit home page to pick up cookies / CSRF
        session.get(WSJ_HOME_URL, timeout=15)

        # Step 2: submit credentials
        payload = {
            "username": email_addr,
            "password": password,
            "cookieConsent": "1",
        }
        resp = session.post(WSJ_LOGIN_URL, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "success" or "access_token" in data:
            logger.info("WSJ login successful.")
            return True
        logger.warning(
            "WSJ login returned unexpected response (result=%s). "
            "Proceeding without authentication.",
            data.get("result", "unknown"),
        )
        return False
    except requests.HTTPError as exc:
        logger.warning(
            "WSJ login failed (HTTP %s). Proceeding without authentication. "
            "Section scraping will be unavailable.",
            exc.response.status_code if exc.response else "?",
        )
    except requests.RequestException as exc:
        logger.warning("WSJ login network error: %s. Proceeding without authentication.", exc)
    except Exception as exc:
        logger.warning("WSJ login unexpected error: %s. Proceeding without authentication.", exc)
    return False


# ------------------------------------------------------------------ #
# Public entry point                                                   #
# ------------------------------------------------------------------ #

def fetch_all_articles(config: dict) -> list[Article]:
    """
    Top-level function called by run_digest.py.

    1. Builds a shared requests.Session
    2. Attempts WSJ login (graceful fallback to RSS-only)
    3. Runs RSSFetcher
    4. Optionally runs SectionScraper if pool is thin
    5. Returns de-URL-deduped article list (no scoring yet)
    """
    settings = config.get("settings", {})
    max_age = settings.get("max_age_hours", 48)
    use_scraper = settings.get("use_section_scraper", True)

    session = _build_session()
    authenticated = _login(session)

    # RSS pass
    rss = RSSFetcher(session, config)
    articles = rss.fetch()
    logger.info("RSS fetch: %d raw candidates", len(articles))

    # Section scraper pass (only if authenticated and enabled)
    if use_scraper and authenticated:
        scraper = SectionScraper(session, config)
        scraped = scraper.fetch()
        logger.info("Section scrape: %d additional candidates", len(scraped))
        articles.extend(scraped)
    elif use_scraper and not authenticated:
        logger.info("Skipping section scraper (not authenticated).")

    # Playwright pass (opt-in)
    if os.environ.get("USE_PLAYWRIGHT") == "1":
        try:
            pf = PlaywrightFetcher(session, config)
            playwright_articles = pf.fetch()
            articles.extend(playwright_articles)
        except (NotImplementedError, RuntimeError) as exc:
            logger.warning("PlaywrightFetcher: %s", exc)

    # Age filter
    before_age = len(articles)
    articles = [a for a in articles if a.age_hours() <= max_age]
    dropped = before_age - len(articles)
    if dropped:
        logger.info("Age filter dropped %d articles (> %sh old).", dropped, max_age)

    # URL dedup (exact)
    seen_urls: set[str] = set()
    unique: list[Article] = []
    for a in articles:
        if a.url not in seen_urls:
            seen_urls.add(a.url)
            unique.append(a)
    logger.info("After URL dedup: %d candidates", len(unique))

    return unique


# ------------------------------------------------------------------ #
# Utility helpers                                                      #
# ------------------------------------------------------------------ #

def _parse_pubdate(item) -> datetime:
    """Parse <pubDate> from an RSS item tag. Falls back to UTC now."""
    pub_tag = item.find("pubDate")
    if pub_tag:
        raw = pub_tag.get_text(strip=True)
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    # Try <dc:date> or <published>
    for tag_name in ("dc:date", "published", "updated"):
        tag = item.find(tag_name)
        if tag:
            raw = tag.get_text(strip=True)
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _strip_html_tags(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", " ", text)


def _clean_url(url: str) -> str:
    """Strip common tracking parameters and normalise WSJ article URLs."""
    # Remove query string tracking params but keep fragment
    base = url.split("?")[0]
    # Ensure HTTPS
    base = base.replace("http://", "https://", 1)
    return base.strip()

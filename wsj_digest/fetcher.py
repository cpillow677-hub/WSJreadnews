"""
Fetcher — collects Article candidates from WSJ RSS feeds and (optionally)
authenticated section pages, then enriches selected articles with full text.

Public interface:
    fetch_all_articles(config: dict) -> list[Article]
    _fetch_all_articles_with_session(config: dict) -> tuple[list[Article], requests.Session]
    enrich_with_full_text(articles_by_category, session, config) -> dict

Architecture:
    RSSFetcher              — requests + BeautifulSoup XML parsing of DJ/WSJ RSS feeds
    SectionScraper          — requests + BeautifulSoup HTML scraping of wsj.com sections
    PlaywrightArticleFetcher— Chromium browser; login once, fetch full article pages
"""
from __future__ import annotations

import email.utils
import logging
import os
import re
import time
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
# WSJ endpoints                                                        #
# ------------------------------------------------------------------ #
WSJ_HOME_URL     = "https://www.wsj.com"
WSJ_LOGIN_URL    = "https://id.wsj.com/auth/submitlogin.json"
WSJ_LOGIN_PAGE   = "https://www.wsj.com/login"

# Playwright selectors for WSJ login form
WSJ_EMAIL_SEL    = 'input[name="username"], input[type="email"], #username'
WSJ_PASSWORD_SEL = 'input[name="password"], input[type="password"], #password'
WSJ_SUBMIT_SEL   = 'button[type="submit"], input[type="submit"]'

# Playwright selector to detect article content has loaded
WSJ_ARTICLE_SEL  = "article, [class*='articleBody'], [class*='article-body']"

# Section URLs for HTML scraping fallback
SECTION_URLS = {
    "world":    "https://www.wsj.com/world",
    "markets":  "https://www.wsj.com/markets",
    "tech":     "https://www.wsj.com/tech",
    "business": "https://www.wsj.com/business",
}

# Current financial news RSS feeds (feeds.a.dj.com was frozen at 2025-01-27)
DEFAULT_RSS_FEEDS = [
    # Yahoo Finance — broad financial/market/tech/world news, updated in real-time
    {"url": "https://finance.yahoo.com/news/rssindex",                            "section": "top",      "name": "Yahoo Finance"},
    # CNBC business and markets sections
    {"url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",              "section": "business", "name": "CNBC Business"},
    {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",             "section": "markets",  "name": "CNBC Markets"},
    {"url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",              "section": "world",    "name": "CNBC World"},
    {"url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",              "section": "tech",     "name": "CNBC Technology"},
    # CNN business and technology
    {"url": "https://rss.cnn.com/rss/money_news_international.rss",              "section": "business", "name": "CNN Business"},
    {"url": "https://rss.cnn.com/rss/cnn_tech.rss",                              "section": "tech",     "name": "CNN Tech"},
    # The Economist — finance and business sections
    {"url": "https://www.economist.com/finance-and-economics/rss.xml",           "section": "markets",  "name": "The Economist Finance"},
    {"url": "https://www.economist.com/business/rss.xml",                        "section": "business", "name": "The Economist Business"},
]

# CSS selector cascade for WSJ article body (used by both requests and Playwright paths)
_WSJ_BODY_SELECTORS = [
    "article",
    "[class*='articleBody']",
    "[class*='article-body']",
    "[class*='ArticleBody']",
]

_WSJ_PARAGRAPH_SELECTORS = [
    "p[class*='paragraph']",
    "p[class*='Paragraph']",
    "div.article-content p",
    "div[class*='body'] p",
    "div[class*='Body'] p",
]

_MIN_PARAGRAPH_CHARS = 80

_BODY_BOILERPLATE_RE = re.compile(
    r"(subscribe now|sign in|log in|wsj\.com|wall street journal|"
    r"©\s*\d{4}|all rights reserved|read more|click here)",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ #
# HTML body parser (shared between requests + Playwright paths)        #
# ------------------------------------------------------------------ #

def _parse_wsj_body(html: str) -> str:
    """
    Extract article body text from WSJ article HTML using a defensive
    selector cascade.  Returns joined paragraph text, or "" if nothing
    meaningful is found.

    Strategy:
      1. Isolate the article container via _WSJ_BODY_SELECTORS.
      2. Within that container, try each _WSJ_PARAGRAPH_SELECTORS.
      3. Fallback: any <p> tag longer than _MIN_PARAGRAPH_CHARS chars.
      4. Strip boilerplate and join with double newline.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Step 1: isolate article container
    container = None
    for sel in _WSJ_BODY_SELECTORS:
        container = soup.select_one(sel)
        if container:
            break
    scope = container if container else soup

    # Step 2: try paragraph selectors
    paragraphs: list[str] = []
    for sel in _WSJ_PARAGRAPH_SELECTORS:
        tags = scope.select(sel)
        if tags:
            paragraphs = [t.get_text(separator=" ", strip=True) for t in tags]
            break

    # Step 3: fallback — any <p> longer than threshold
    if not paragraphs:
        paragraphs = [
            p.get_text(separator=" ", strip=True)
            for p in scope.find_all("p")
            if len(p.get_text(strip=True)) > _MIN_PARAGRAPH_CHARS
        ]

    # Step 4: filter boilerplate
    clean = [
        p for p in paragraphs
        if not _BODY_BOILERPLATE_RE.search(p) and len(p) > 30
    ]
    return "\n\n".join(clean)


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

        link_tag = item.find("link")
        url = ""
        if link_tag:
            url = link_tag.get_text(strip=True) or link_tag.get("href", "")
        if not url:
            guid_tag = item.find("guid")
            if guid_tag:
                candidate = guid_tag.get_text(strip=True)
                if candidate.startswith("http"):
                    url = candidate
        if not url:
            return None
        url = _clean_url(url)

        lead_text = ""
        desc_tag = item.find("description")
        if desc_tag:
            raw_desc = desc_tag.get_text(separator=" ", strip=True)
            lead_text = _strip_html_tags(raw_desc).strip()

        author = ""
        for tag_name in ("dc:creator", "author", "creator"):
            author_tag = item.find(tag_name)
            if author_tag:
                author = author_tag.get_text(strip=True)
                break

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
    """Authenticated HTML scraper for wsj.com section pages."""

    def fetch(self) -> list[Article]:
        articles: list[Article] = []
        for section_name, url in SECTION_URLS.items():
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

        article_tags = soup.find_all("article")
        if not article_tags:
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

        title = anchor.get_text(strip=True)
        if not title or len(title) < 10:
            heading = tag.find(["h2", "h3", "h4"])
            if heading:
                title = heading.get_text(strip=True)
        if not title or len(title) < 10:
            return None

        lead_text = ""
        para = tag.find("p")
        if para:
            lead_text = para.get_text(strip=True)

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
# Playwright Article Fetcher                                           #
# ------------------------------------------------------------------ #
class PlaywrightArticleFetcher:
    """
    Uses a headless Chromium browser to log in to WSJ and fetch full article text.

    WSJ increasingly renders articles with React/JS. This class:
      1. Launches a single Chromium browser instance
      2. Logs in once using WSJ_EMAIL / WSJ_PASSWORD env vars
      3. Fetches each article URL, waits for JS to render, extracts body text
      4. Cleans up on close

    Usage (context manager recommended):
        with PlaywrightArticleFetcher(config) as fetcher:
            text = fetcher.fetch_article(url)

    Requirements:
        pip install playwright
        playwright install chromium
    """

    def __init__(self, config: dict):
        self.config   = config
        settings      = config.get("settings", {})
        self.headless = bool(settings.get("playwright_headless", True))
        self.timeout  = int(settings.get("playwright_timeout_ms", 30_000))
        self.rate_sec = float(settings.get("full_text_rate_sec", 1.0))
        self._pw      = None
        self._browser = None
        self._context = None

    # ---- lifecycle ----

    def start(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed.\n"
                "Run:  pip install playwright && playwright install chromium"
            )
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        self._login()

    def stop(self) -> None:
        for obj in (self._context, self._browser, self._pw):
            if obj is not None:
                try:
                    obj.close() if hasattr(obj, "close") else obj.stop()
                except Exception as exc:
                    logger.debug("Playwright cleanup error: %s", exc)

    def __enter__(self) -> "PlaywrightArticleFetcher":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ---- login ----

    def _login(self) -> None:
        """Navigate to WSJ login page and authenticate via browser form."""
        email    = os.environ.get("WSJ_EMAIL", "")
        password = os.environ.get("WSJ_PASSWORD", "")
        if not email or not password:
            logger.warning(
                "WSJ_EMAIL / WSJ_PASSWORD not set — Playwright running unauthenticated. "
                "Full article text may be limited to preview paragraphs."
            )
            return

        page = self._context.new_page()
        try:
            page.goto(WSJ_LOGIN_PAGE, wait_until="domcontentloaded", timeout=self.timeout)

            # Fill email field
            page.wait_for_selector(WSJ_EMAIL_SEL, timeout=self.timeout)
            page.fill(WSJ_EMAIL_SEL, email)

            # Handle two-step forms (email → Continue → password)
            continue_btn = page.query_selector(
                'button:has-text("Continue"), button:has-text("Next")'
            )
            if continue_btn:
                continue_btn.click()
                page.wait_for_timeout(1500)

            # Fill password and submit
            page.wait_for_selector(WSJ_PASSWORD_SEL, timeout=self.timeout)
            page.fill(WSJ_PASSWORD_SEL, password)
            page.click(WSJ_SUBMIT_SEL)

            # Wait for redirect away from login/signin pages
            page.wait_for_url(
                lambda u: "login" not in u and "signin" not in u,
                timeout=self.timeout,
            )
            logger.info("Playwright WSJ login successful.")
        except Exception as exc:
            logger.warning(
                "Playwright WSJ login failed: %s — continuing unauthenticated.", exc
            )
        finally:
            page.close()

    # ---- article fetching ----

    def fetch_article(self, url: str) -> str:
        """
        Navigate to url in a new browser tab, wait for article JS to render,
        extract and return body text via _parse_wsj_body().
        Returns "" on any error — never raises.
        """
        if self._context is None:
            logger.warning("PlaywrightArticleFetcher.fetch_article called before start()")
            return ""

        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            # Best-effort wait for article content selector
            try:
                page.wait_for_selector(WSJ_ARTICLE_SEL, timeout=10_000)
            except Exception:
                pass  # proceed with whatever rendered
            return _parse_wsj_body(page.content())
        except Exception as exc:
            logger.warning("Playwright fetch failed for %s: %s", url, exc)
            return ""
        finally:
            page.close()


# ------------------------------------------------------------------ #
# Authentication helpers (requests path)                               #
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
    Attempt to authenticate the requests session against WSJ.
    Reads WSJ_EMAIL and WSJ_PASSWORD from environment.
    Returns True on success, False on any failure.
    """
    email_addr = os.environ.get("WSJ_EMAIL", "")
    password   = os.environ.get("WSJ_PASSWORD", "")
    if not email_addr or not password:
        logger.info(
            "WSJ_EMAIL / WSJ_PASSWORD not set — running in RSS-only (unauthenticated) mode."
        )
        return False

    try:
        session.get(WSJ_HOME_URL, timeout=15)
        payload = {"username": email_addr, "password": password, "cookieConsent": "1"}
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
            "WSJ login failed (HTTP %s). Proceeding without authentication.",
            exc.response.status_code if exc.response else "?",
        )
    except requests.RequestException as exc:
        logger.warning("WSJ login network error: %s. Proceeding without authentication.", exc)
    except Exception as exc:
        logger.warning("WSJ login unexpected error: %s. Proceeding without authentication.", exc)
    return False


# ------------------------------------------------------------------ #
# Full-text enrichment                                                 #
# ------------------------------------------------------------------ #

def enrich_with_full_text(
    articles_by_category: dict[str, list[Article]],
    session: requests.Session,
    config: dict,
) -> dict[str, list[Article]]:
    """
    Fetch the full body text for each selected article and store it in
    article.full_text.  Called AFTER select_top_articles() so only the
    ~12 selected articles are fetched, not all candidates.

    Chooses Playwright or requests based on:
        USE_PLAYWRIGHT=1  (env var)   OR
        settings.use_playwright_fulltext: true  (config)

    Falls back gracefully to lead_text if any fetch fails.
    """
    settings  = config.get("settings", {})
    use_pw    = (
        os.environ.get("USE_PLAYWRIGHT") == "1"
        or bool(settings.get("use_playwright_fulltext", False))
    )
    timeout   = int(settings.get("full_text_timeout", 15))
    rate_sec  = float(settings.get("full_text_rate_sec", 1.0))

    all_articles: list[Article] = [
        a for arts in articles_by_category.values() for a in arts
    ]
    total = len(all_articles)

    if use_pw:
        _enrich_playwright(all_articles, config, rate_sec)
    else:
        _enrich_requests(all_articles, session, timeout, rate_sec)

    enriched = sum(1 for a in all_articles if a.full_text)
    logger.info("Full-text enrichment complete: %d/%d articles enriched", enriched, total)
    return articles_by_category


def _enrich_playwright(
    articles: list[Article],
    config: dict,
    rate_sec: float,
) -> None:
    """Fetch article bodies using Playwright (handles JS-rendered content)."""
    total = len(articles)
    logger.info("Full-text enrichment: using Playwright for %d articles", total)
    try:
        with PlaywrightArticleFetcher(config) as pw:
            for idx, article in enumerate(articles):
                if article.full_text_fetched:
                    continue
                try:
                    article.full_text = pw.fetch_article(article.url)
                    logger.debug(
                        "[%d/%d] Playwright: %d chars — %s",
                        idx + 1, total, len(article.full_text), article.url,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%d/%d] Playwright article error: %s", idx + 1, total, exc
                    )
                    article.fetch_error     = True
                    article.fetch_error_msg = str(exc)
                finally:
                    article.full_text_fetched = True
                if idx < total - 1 and rate_sec > 0:
                    time.sleep(rate_sec)
    except RuntimeError as exc:
        logger.error(
            "Playwright unavailable: %s\n"
            "Install with: pip install playwright && playwright install chromium",
            exc,
        )


def _enrich_requests(
    articles: list[Article],
    session: requests.Session,
    timeout: int,
    rate_sec: float,
) -> None:
    """Fetch article bodies using the existing requests session (static HTML)."""
    total = len(articles)
    logger.info("Full-text enrichment: using requests for %d articles", total)
    for idx, article in enumerate(articles):
        if article.full_text_fetched:
            continue
        try:
            resp = session.get(article.url, timeout=timeout)
            resp.raise_for_status()
            article.full_text = _parse_wsj_body(resp.text)
            logger.debug(
                "[%d/%d] requests: %d chars — %s",
                idx + 1, total, len(article.full_text), article.url,
            )
        except requests.Timeout:
            logger.warning(
                "[%d/%d] Timeout (%ds) fetching full text: %s",
                idx + 1, total, timeout, article.url,
            )
            article.fetch_error     = True
            article.fetch_error_msg = f"timeout after {timeout}s"
        except requests.HTTPError as exc:
            logger.warning(
                "[%d/%d] HTTP %s fetching full text: %s",
                idx + 1, total,
                exc.response.status_code if exc.response else "?",
                article.url,
            )
            article.fetch_error     = True
            article.fetch_error_msg = str(exc)
        except Exception as exc:
            logger.warning(
                "[%d/%d] Unexpected error fetching full text: %s — %s",
                idx + 1, total, article.url, exc,
            )
            article.fetch_error     = True
            article.fetch_error_msg = str(exc)
        finally:
            article.full_text_fetched = True
        if idx < total - 1 and rate_sec > 0:
            time.sleep(rate_sec)


# ------------------------------------------------------------------ #
# Public entry points                                                  #
# ------------------------------------------------------------------ #

def _fetch_all_articles_with_session(
    config: dict,
) -> tuple[list[Article], requests.Session]:
    """
    Internal variant of fetch_all_articles that also returns the authenticated
    requests.Session for downstream use (e.g., enrich_with_full_text requests path).
    """
    settings  = config.get("settings", {})
    max_age   = settings.get("max_age_hours", 48)
    use_scraper = settings.get("use_section_scraper", True)

    session       = _build_session()
    authenticated = _login(session)

    # RSS pass
    rss      = RSSFetcher(session, config)
    articles = rss.fetch()
    logger.info("RSS fetch: %d raw candidates", len(articles))

    # Section scraper pass (run always when enabled; auth improves results but isn't required)
    if use_scraper:
        scraper = SectionScraper(session, config)
        scraped = scraper.fetch()
        logger.info("Section scrape: %d additional candidates", len(scraped))
        articles.extend(scraped)

    # Age filter — with fallback for system clock / feed date mismatch.
    # When the system clock is ahead of the feed (e.g. a dev environment
    # with a future system date), all articles appear "too old" and the
    # pipeline produces nothing.  In that case we fall back to
    # article-relative dating: keep articles within max_age hours of the
    # newest article in the feed rather than within max_age hours of now.
    before_age = len(articles)

    def _pub_utc(a: Article) -> datetime:
        t = a.publish_time
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    filtered = [a for a in articles if a.age_hours() <= max_age]

    if not filtered and articles:
        now          = datetime.now(timezone.utc)
        newest_time  = max(_pub_utc(a) for a in articles)
        clock_lead_h = (now - newest_time).total_seconds() / 3600
        filtered     = [
            a for a in articles
            if (newest_time - _pub_utc(a)).total_seconds() / 3600 <= max_age
        ]
        logger.warning(
            "Age filter: system clock (%s UTC) is %.0fh ahead of the newest "
            "feed article (%s UTC). Switching to article-relative cutoff — "
            "%d of %d articles kept.",
            now.strftime("%Y-%m-%d %H:%M"),
            clock_lead_h,
            newest_time.strftime("%Y-%m-%d %H:%M"),
            len(filtered),
            before_age,
        )
    else:
        dropped = before_age - len(filtered)
        if dropped:
            logger.info("Age filter dropped %d articles (> %sh old).", dropped, max_age)

    articles = filtered

    # URL dedup (exact)
    seen_urls: set[str] = set()
    unique: list[Article] = []
    for a in articles:
        if a.url not in seen_urls:
            seen_urls.add(a.url)
            unique.append(a)
    logger.info("After URL dedup: %d candidates", len(unique))

    return unique, session


def fetch_all_articles(config: dict) -> list[Article]:
    """
    Top-level function for backward compatibility.
    Calls _fetch_all_articles_with_session and discards the session.
    """
    articles, _ = _fetch_all_articles_with_session(config)
    return articles


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
    for tag_name in ("dc:date", "published", "updated"):
        tag = item.find(tag_name)
        if tag:
            raw = tag.get_text(strip=True)
            try:
                return datetime.fromisoformat(
                    raw.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def _clean_url(url: str) -> str:
    base = url.split("?")[0]
    base = base.replace("http://", "https://", 1)
    return base.strip()

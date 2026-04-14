"""
Unit tests for the WSJ Digest pipeline.

All HTTP calls are mocked — no network access required.

Run with:
    pytest tests/test_pipeline.py -v
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Make the project root importable when running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from wsj_digest.models import Article
from wsj_digest.scorer import score_articles, classify_article, _compute_recency
from wsj_digest.selector import deduplicate_by_url, deduplicate_fuzzy, select_top_articles
from wsj_digest.summarizer import summarize_article, summarize_all, _build_summary
from wsj_digest.renderer import render_html, render_markdown

# ------------------------------------------------------------------ #
# Fixtures & helpers                                                    #
# ------------------------------------------------------------------ #

SAMPLE_CONFIG_PATH = Path(__file__).parent.parent / "config" / "categories.yaml"


def load_config() -> dict:
    with SAMPLE_CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


def make_article(**kwargs) -> Article:
    defaults = {
        "title": "Test Article Headline",
        "url": "https://www.wsj.com/articles/test-article",
        "source_section": "markets",
        "source_feed": "WSJ Markets Main",
        "publish_time": datetime.now(timezone.utc),
        "lead_text": "This is a test lead paragraph about financial markets and interest rates.",
    }
    defaults.update(kwargs)
    return Article(**defaults)


# ------------------------------------------------------------------ #
# models.Article                                                        #
# ------------------------------------------------------------------ #

class TestArticleModel:
    def test_age_hours_recent(self):
        article = make_article(publish_time=datetime.now(timezone.utc) - timedelta(hours=1))
        assert 0.9 < article.age_hours() < 1.1

    def test_age_hours_naive_datetime(self):
        """Naive datetime (no tz) should be treated as UTC without raising."""
        naive_dt = datetime.utcnow() - timedelta(hours=3)
        article = make_article(publish_time=naive_dt)
        assert 2.9 < article.age_hours() < 3.1

    def test_to_dict_keys(self):
        article = make_article()
        d = article.to_dict()
        for key in ("title", "url", "source_section", "publish_time", "summary", "why_it_matters"):
            assert key in d


# ------------------------------------------------------------------ #
# scorer.py — recency                                                   #
# ------------------------------------------------------------------ #

class TestRecencyScore:
    def _article_aged(self, hours: float) -> Article:
        return make_article(
            publish_time=datetime.now(timezone.utc) - timedelta(hours=hours)
        )

    def test_fresh(self):
        assert _compute_recency(self._article_aged(0.5)) == 100.0

    def test_two_hours(self):
        # Use 1.99h to avoid microsecond drift pushing us past the ≤2h boundary
        assert _compute_recency(self._article_aged(1.99)) == 100.0

    def test_four_hours(self):
        score = _compute_recency(self._article_aged(4.0))
        assert 70 <= score <= 90

    def test_twelve_hours(self):
        score = _compute_recency(self._article_aged(12.0))
        assert 44 <= score <= 50

    def test_twenty_four_hours(self):
        score = _compute_recency(self._article_aged(24.0))
        assert 14 <= score <= 18

    def test_stale_beyond_48h(self):
        assert _compute_recency(self._article_aged(49.0)) == 0.0


# ------------------------------------------------------------------ #
# scorer.py — classification                                            #
# ------------------------------------------------------------------ #

class TestClassification:
    def setup_method(self):
        self.config = load_config()
        self.cats = self.config["categories"]

    def test_market_article(self):
        article = make_article(
            title="Federal Reserve Raises Interest Rates 25 Basis Points",
            lead_text="The Fed raised its benchmark rate amid S&P 500 selloff and Treasury yield surge.",
        )
        cat, score = classify_article(article, self.cats)
        assert cat == "Market", f"Expected Market, got {cat}"
        assert score > 0

    def test_tech_article(self):
        article = make_article(
            title="Nvidia Unveils New GPU for Artificial Intelligence Data Centers",
            lead_text="The semiconductor maker announced a new AI chip targeting cloud computing workloads.",
        )
        cat, score = classify_article(article, self.cats)
        assert cat == "Tech", f"Expected Tech, got {cat}"

    def test_stock_article(self):
        article = make_article(
            title="Apple Reports Record Q2 Earnings Beating Revenue Estimates",
            lead_text="Apple's quarterly EPS beat analyst expectations. The company issued strong guidance.",
        )
        cat, score = classify_article(article, self.cats)
        assert cat == "Stock", f"Expected Stock, got {cat}"

    def test_global_article(self):
        article = make_article(
            title="US and China Trade War Escalates With New Tariffs",
            lead_text="Geopolitics intensified as bilateral trade sanctions were expanded under the WTO framework.",
        )
        cat, score = classify_article(article, self.cats)
        assert cat == "Global", f"Expected Global, got {cat}"

    def test_no_match_returns_none(self):
        article = make_article(
            title="Local Weather in Springfield",
            lead_text="It rained yesterday.",
        )
        cat, score = classify_article(article, self.cats)
        assert cat is None
        assert score == 0.0


# ------------------------------------------------------------------ #
# scorer.py — full score_articles                                       #
# ------------------------------------------------------------------ #

class TestScoreArticles:
    def setup_method(self):
        self.config = load_config()

    def test_sorted_descending(self):
        articles = [
            make_article(title="Apple Earnings Beat Revenue Estimates Q2",
                         lead_text="Earnings EPS revenue profit guidance beat"),
            make_article(title="Local News Story",
                         lead_text="Nothing of note happened today."),
        ]
        result = score_articles(articles, self.config)
        assert result[0].total_score >= result[1].total_score

    def test_all_fields_populated(self):
        articles = [make_article()]
        result = score_articles(articles, self.config)
        a = result[0]
        assert isinstance(a.importance_score, float)
        assert isinstance(a.recency_score, float)
        assert isinstance(a.market_relevance_score, float)
        assert isinstance(a.total_score, float)

    def test_importance_for_keyword_rich_article(self):
        article = make_article(
            title="Federal Reserve Rate Hike Triggers Bond Yield Surge",
            lead_text="The Fed raised rates. Treasury yields spiked. S&P 500 fell. GDP outlook revised.",
        )
        result = score_articles([article], self.config)
        assert result[0].importance_score > 50


# ------------------------------------------------------------------ #
# selector.py — deduplication                                           #
# ------------------------------------------------------------------ #

class TestDeduplication:
    def test_exact_url_dedup(self):
        url = "https://www.wsj.com/articles/same"
        a1 = make_article(url=url, title="Article A")
        a2 = make_article(url=url, title="Article A Duplicate")
        result = deduplicate_by_url([a1, a2])
        assert len(result) == 1
        assert result[0].url == url

    def test_fuzzy_same_title_different_order(self):
        a1 = make_article(
            title="Apple Earnings Beat: Stock Rises After Strong Quarter",
            url="https://www.wsj.com/articles/a1",
            total_score=80.0,
        )
        a2 = make_article(
            title="Stock Rises After Apple Earnings Beat Strong Quarter",
            url="https://www.wsj.com/articles/a2",
            total_score=60.0,
        )
        result = deduplicate_fuzzy([a1, a2], threshold=85)
        assert len(result) == 1
        assert result[0].url == a1.url  # higher-scored kept

    def test_fuzzy_distinct_titles_both_kept(self):
        a1 = make_article(
            title="Apple Reports Q1 Earnings Beat",
            url="https://www.wsj.com/articles/apple",
        )
        a2 = make_article(
            title="Federal Reserve Raises Interest Rates",
            url="https://www.wsj.com/articles/fed",
        )
        result = deduplicate_fuzzy([a1, a2], threshold=85)
        assert len(result) == 2


# ------------------------------------------------------------------ #
# selector.py — select_top_articles                                     #
# ------------------------------------------------------------------ #

class TestSelectTopArticles:
    def setup_method(self):
        self.config = load_config()
        self.cats = self.config["categories"]

    # Distinct titles per category so fuzzy dedup doesn't collapse them
    _TITLES: dict[str, list[str]] = {
        "Global": [
            "US-China Trade War Escalates With New Tariffs on Technology Goods",
            "ECB Holds Rates Amid Eurozone Slowdown and Cooling Inflation Data",
            "India Overtakes Germany as World Third Largest Economy IMF Says",
            "NATO Summit Reaches Agreement on Defence Spending Commitments",
            "Russia Sanctions Tightened as G7 Aligns on New Export Controls",
        ],
        "Market": [
            "Federal Reserve Raises Benchmark Rate 25 Basis Points Signals Pause",
            "Oil Prices Fall Four Percent on Weakening Global Demand Outlook",
            "Ten Year Treasury Yield Climbs to 5.2 Percent Bond Selloff Deepens",
            "Dollar Index Hits Two Year High on Safe Haven Demand Surge",
            "Gold Prices Rise Sharply as Investors Seek Inflation Hedge",
        ],
        "Stock": [
            "Apple Reports Record Second Quarter Earnings Beating Revenue Estimates",
            "ExxonMobil to Acquire Pioneer Natural Resources for 65 Billion Dollars",
            "Tesla Downgraded by Goldman Sachs on Slowing Electric Vehicle Demand",
            "Microsoft Azure Cloud Revenue Grows 28 Percent in Fiscal Quarter",
            "Amazon Announces CEO Change as Andy Jassy Restructures Operations",
        ],
        "Tech": [
            "Nvidia Announces H300 AI Chip With Threefold Performance Improvement",
            "European Union Fines Google Four Billion Euros for Android Antitrust",
            "OpenAI Raises Five Billion Dollars in Series F Funding at 150 Billion Valuation",
            "Meta Releases Open Source Large Language Model Beating GPT Benchmarks",
            "TSMC Plans New Arizona Semiconductor Fabrication Plant Expansion",
        ],
    }

    def _make_articles_for_category(self, cat: str, n: int) -> list[Article]:
        articles = []
        titles = self._TITLES.get(cat, [f"{cat} Story {i}" for i in range(n)])
        for i in range(n):
            title = titles[i] if i < len(titles) else f"{cat} Unique Story Number {i}"
            a = make_article(
                title=title,
                url=f"https://www.wsj.com/articles/{cat.lower()}-{i}",
            )
            a.category = cat
            a.total_score = float(100 - i)
            articles.append(a)
        return articles

    def test_full_selection(self):
        articles = []
        for cat in ["Global", "Market", "Stock", "Tech"]:
            articles.extend(self._make_articles_for_category(cat, 5))
        result = select_top_articles(articles, self.cats, self.config)
        for cat_name in ["Global", "Market", "Stock", "Tech"]:
            assert len(result[cat_name]) == 3

    def test_shortfall_logged(self, caplog):
        articles = self._make_articles_for_category("Market", 2)
        for cat in ["Global", "Stock", "Tech"]:
            articles.extend(self._make_articles_for_category(cat, 3))
        with caplog.at_level(logging.WARNING):
            result = select_top_articles(articles, self.cats, self.config)
        assert len(result["Market"]) == 2
        assert any("SHORTFALL" in r.message for r in caplog.records)

    def test_no_duplicates_in_selection(self):
        url = "https://www.wsj.com/articles/dup"
        a1 = make_article(url=url, title="Market Article")
        a1.category = "Market"
        a1.total_score = 90.0
        a2 = make_article(url=url, title="Market Article Dup")
        a2.category = "Market"
        a2.total_score = 80.0
        result = select_top_articles([a1, a2], self.cats, self.config)
        urls = [a.url for a in result.get("Market", [])]
        assert len(urls) == len(set(urls))


# ------------------------------------------------------------------ #
# summarizer.py                                                         #
# ------------------------------------------------------------------ #

class TestSummarizer:
    def setup_method(self):
        self.config = load_config()
        self.cats = self.config["categories"]

    def test_word_count_bounds(self):
        article = make_article(
            title="Federal Reserve Raises Rates Third Time This Year",
            lead_text=(
                "The Federal Reserve raised its benchmark interest rate by 25 basis points, "
                "citing persistent inflation above its 2% target. The move pushes the federal "
                "funds rate to its highest level in 15 years, affecting mortgage rates and "
                "corporate borrowing costs."
            ),
        )
        article.category = "Market"
        result = summarize_article(article, self.cats)
        word_count = len(result.summary.split())
        assert 100 <= word_count <= 150, f"Word count {word_count} outside 100-150"

    def test_no_crash_empty_lead(self):
        article = make_article(title="Some WSJ Headline", lead_text="")
        article.category = "Stock"
        result = summarize_article(article, self.cats)
        assert result.summary  # non-empty
        assert result.why_it_matters  # at least one bullet

    def test_why_it_matters_list(self):
        article = make_article()
        article.category = "Tech"
        result = summarize_article(article, self.cats)
        assert isinstance(result.why_it_matters, list)
        assert len(result.why_it_matters) >= 1

    def test_no_full_article_text(self):
        """Summary must be derived from title/lead only — check it is not a reproduction of
        very long boilerplate text."""
        long_text = "WSJ article text. " * 500  # simulate a full article accidentally passed
        article = make_article(lead_text=long_text[:300])  # only first 300 chars as lead
        article.category = "Global"
        result = summarize_article(article, self.cats)
        assert len(result.summary.split()) <= 200  # hard ceiling


# ------------------------------------------------------------------ #
# renderer.py                                                           #
# ------------------------------------------------------------------ #

class TestRenderer:
    def setup_method(self):
        self.config = load_config()
        self.cats = self.config["categories"]

    def _build_articles_by_category(self) -> dict[str, list[Article]]:
        result = {}
        for cat in ["Global", "Market", "Stock", "Tech"]:
            articles = []
            for i in range(3):
                a = make_article(
                    title=f"{cat} Story {i+1}: Important Development",
                    url=f"https://www.wsj.com/articles/{cat.lower()}-{i}",
                )
                a.category = cat
                a.total_score = float(90 - i * 5)
                a.summary = (
                    f"This is a {cat} story summary. " * 8
                ).strip()
                a.why_it_matters = [
                    f"Affects {cat} markets broadly.",
                    "Investors should monitor related developments.",
                ]
                articles.append(a)
            result[cat] = articles
        return result

    def test_render_html_creates_file(self, tmp_path):
        articles = self._build_articles_by_category()
        out = render_html(articles, self.cats, self.config, tmp_path, "2026-04-14")
        assert out.exists()
        content = out.read_text()
        assert "<html" in content
        assert "WSJ Daily Digest" in content
        assert "2026-04-14" in str(out)

    def test_render_html_contains_categories(self, tmp_path):
        articles = self._build_articles_by_category()
        out = render_html(articles, self.cats, self.config, tmp_path, "2026-04-14")
        content = out.read_text()
        for cat in ["Global", "Market", "Stock", "Tech"]:
            assert cat in content

    def test_render_html_has_clickable_links(self, tmp_path):
        articles = self._build_articles_by_category()
        out = render_html(articles, self.cats, self.config, tmp_path, "2026-04-14")
        content = out.read_text()
        assert 'href="https://www.wsj.com/articles/' in content

    def test_render_markdown_creates_file(self, tmp_path):
        articles = self._build_articles_by_category()
        out = render_markdown(articles, self.cats, self.config, tmp_path, "2026-04-14")
        assert out.exists()
        content = out.read_text()
        assert content.startswith("# WSJ Daily Digest")

    def test_render_markdown_contains_all_categories(self, tmp_path):
        articles = self._build_articles_by_category()
        out = render_markdown(articles, self.cats, self.config, tmp_path, "2026-04-14")
        content = out.read_text()
        for cat in ["Global", "Market", "Stock", "Tech"]:
            assert cat in content


# ------------------------------------------------------------------ #
# End-to-end pipeline with mock HTTP                                    #
# ------------------------------------------------------------------ #

MOCK_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>WSJ.com: World News</title>
    <link>https://www.wsj.com/world</link>

    <item>
      <title>Federal Reserve Raises Rates for Third Time This Year</title>
      <link>https://www.wsj.com/articles/fed-raises-rates-2026</link>
      <description>The Federal Reserve raised its benchmark interest rate by 25 basis points citing persistent inflation above its 2 percent target. The move pushes the federal funds rate to its highest level in 15 years. Treasury yields surged and the S&amp;P 500 fell.</description>
      <pubDate>Mon, 14 Apr 2026 04:00:00 +0000</pubDate>
      <dc:creator>Nick Timiraos</dc:creator>
    </item>

    <item>
      <title>US-China Trade War Escalates With New Tariffs on Technology Exports</title>
      <link>https://www.wsj.com/articles/us-china-tariffs-2026</link>
      <description>Washington and Beijing escalated their trade war with a new round of tariffs targeting semiconductor exports and geopolitical tensions rose sharply. The WTO was called to intervene in the bilateral dispute.</description>
      <pubDate>Mon, 14 Apr 2026 03:00:00 +0000</pubDate>
      <dc:creator>Lingling Wei</dc:creator>
    </item>

    <item>
      <title>Apple Reports Record Q2 Earnings Beating Revenue Estimates</title>
      <link>https://www.wsj.com/articles/apple-earnings-q2-2026</link>
      <description>Apple quarterly earnings beat analyst expectations with record revenue and EPS. The company issued strong guidance for the next quarter. Shares rose in after-hours trading.</description>
      <pubDate>Mon, 14 Apr 2026 02:30:00 +0000</pubDate>
      <dc:creator>Aaron Tilley</dc:creator>
    </item>

    <item>
      <title>Nvidia Unveils H300 AI Chip for Data Centers and Cloud Computing</title>
      <link>https://www.wsj.com/articles/nvidia-h300-chip-2026</link>
      <description>Nvidia announced its next-generation GPU designed for artificial intelligence and machine learning workloads. The semiconductor giant said demand from cloud computing providers remained strong.</description>
      <pubDate>Mon, 14 Apr 2026 02:00:00 +0000</pubDate>
      <dc:creator>Asa Fitch</dc:creator>
    </item>

    <item>
      <title>ECB Holds Interest Rates Amid Eurozone Economic Slowdown</title>
      <link>https://www.wsj.com/articles/ecb-rates-eurozone-2026</link>
      <description>The European Central Bank kept its key interest rates unchanged as eurozone GDP contracted and inflation fell below its target. The geopolitical backdrop in Europe weighed on the economic outlook.</description>
      <pubDate>Mon, 14 Apr 2026 01:30:00 +0000</pubDate>
      <dc:creator>Tom Fairless</dc:creator>
    </item>

    <item>
      <title>Oil Prices Drop 4 Percent on Rising Demand Fears and Dollar Strength</title>
      <link>https://www.wsj.com/articles/oil-price-drop-2026</link>
      <description>Brent crude futures fell sharply amid concerns over weakening global demand and a stronger dollar. Commodity traders cited slowing growth in China and Europe as key factors in the selloff.</description>
      <pubDate>Mon, 14 Apr 2026 01:00:00 +0000</pubDate>
      <dc:creator>Will Horner</dc:creator>
    </item>

    <item>
      <title>Tesla Downgraded by Goldman Sachs on Slowing EV Demand Outlook</title>
      <link>https://www.wsj.com/articles/tesla-downgrade-goldman-2026</link>
      <description>Goldman Sachs analysts cut their rating on Tesla shares to sell and lowered the price target citing deteriorating EV market demand and margin compression. The downgrade sent Tesla stock lower.</description>
      <pubDate>Mon, 14 Apr 2026 00:30:00 +0000</pubDate>
      <dc:creator>Al Root</dc:creator>
    </item>

    <item>
      <title>EU Fines Google 4 Billion Euros for Android Antitrust Violations</title>
      <link>https://www.wsj.com/articles/eu-google-antitrust-fine-2026</link>
      <description>European Union regulators fined Alphabet's Google subsidiary 4 billion euros for anticompetitive practices related to its Android mobile platform. The tech regulation decision follows years of investigation.</description>
      <pubDate>Mon, 14 Apr 2026 00:00:00 +0000</pubDate>
      <dc:creator>Sam Schechner</dc:creator>
    </item>

    <item>
      <title>India Overtakes Germany as World Third Largest Economy</title>
      <link>https://www.wsj.com/articles/india-gdp-global-economy-2026</link>
      <description>India surpassed Germany to become the world third largest economy by nominal GDP according to IMF estimates. The global economic milestone reflects strong growth and a rapidly expanding workforce.</description>
      <pubDate>Sun, 13 Apr 2026 22:00:00 +0000</pubDate>
      <dc:creator>Vibhuti Agarwal</dc:creator>
    </item>

    <item>
      <title>Treasury Yield Hits 5.2 Percent as Bond Selloff Accelerates</title>
      <link>https://www.wsj.com/articles/treasury-yield-52pct-2026</link>
      <description>The 10-year Treasury yield climbed to 5.2 percent as investors sold bonds following the Federal Reserve rate hike. The yield surge reflects repricing of interest rate expectations across fixed income markets.</description>
      <pubDate>Sun, 13 Apr 2026 21:00:00 +0000</pubDate>
      <dc:creator>Sam Goldfarb</dc:creator>
    </item>

    <item>
      <title>ExxonMobil to Acquire Pioneer Natural Resources for 65 Billion Dollars</title>
      <link>https://www.wsj.com/articles/exxon-pioneer-acquisition-2026</link>
      <description>ExxonMobil announced a merger deal to acquire Pioneer Natural Resources in a 65 billion dollar all-stock transaction. The acquisition would create the largest US oil producer and reshape the energy sector.</description>
      <pubDate>Sun, 13 Apr 2026 20:00:00 +0000</pubDate>
      <dc:creator>Collin Eaton</dc:creator>
    </item>

    <item>
      <title>OpenAI Raises 5 Billion Dollars in Series F Funding Round</title>
      <link>https://www.wsj.com/articles/openai-funding-2026</link>
      <description>OpenAI secured 5 billion dollars in new venture capital funding valuing the artificial intelligence startup at 150 billion dollars. The fundraise will accelerate development of next-generation large language models.</description>
      <pubDate>Sun, 13 Apr 2026 19:00:00 +0000</pubDate>
      <dc:creator>Berber Jin</dc:creator>
    </item>

  </channel>
</rss>"""


class TestEndToEndPipeline:
    def setup_method(self):
        self.config = load_config()
        # Force articles to appear recent by overriding publish times
        # (handled by mocking)

    def _make_mock_response(self) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.content = MOCK_RSS_XML.encode("utf-8")
        mock_resp.text = MOCK_RSS_XML
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    @patch("wsj_digest.fetcher._login", return_value=False)
    @patch("requests.Session.get")
    def test_full_pipeline_creates_output_files(self, mock_get, mock_login, tmp_path):
        # All RSS fetches return our mock XML
        mock_get.return_value = self._make_mock_response()

        # Override publish times to be recent (patch age_hours on Article)
        from wsj_digest import fetcher, scorer, selector, summarizer, renderer

        articles = fetcher.fetch_all_articles(self.config)
        # Force articles to appear fresh for scoring
        for a in articles:
            a.publish_time = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )

        articles = scorer.score_articles(articles, self.config)
        articles_by_cat = selector.select_top_articles(
            articles, self.config["categories"], self.config
        )
        articles_by_cat = summarizer.summarize_all(
            articles_by_cat, self.config["categories"]
        )

        html_path = renderer.render_html(
            articles_by_cat, self.config["categories"], self.config, tmp_path, "2026-04-14"
        )
        md_path = renderer.render_markdown(
            articles_by_cat, self.config["categories"], self.config, tmp_path, "2026-04-14"
        )

        assert html_path.exists(), "HTML file not created"
        assert md_path.exists(), "Markdown file not created"

        html = html_path.read_text()
        assert "<html" in html
        for cat in ["Global", "Market", "Stock", "Tech"]:
            assert cat in html

        md = md_path.read_text()
        assert md.startswith("# WSJ Daily Digest")

    @patch("wsj_digest.fetcher._login", return_value=False)
    @patch("requests.Session.get")
    def test_pipeline_handles_empty_feed_gracefully(self, mock_get, mock_login, tmp_path):
        """Empty RSS response should not crash the pipeline."""
        empty_rss = """<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>"""
        mock_resp = MagicMock()
        mock_resp.content = empty_rss.encode()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from wsj_digest import fetcher, scorer, selector, summarizer, renderer

        articles = fetcher.fetch_all_articles(self.config)
        articles = scorer.score_articles(articles, self.config)
        articles_by_cat = selector.select_top_articles(
            articles, self.config["categories"], self.config
        )
        articles_by_cat = summarizer.summarize_all(
            articles_by_cat, self.config["categories"]
        )
        # Should produce output files even with zero articles (shortfall notices)
        html_path = renderer.render_html(
            articles_by_cat, self.config["categories"], self.config, tmp_path, "2026-04-14"
        )
        assert html_path.exists()
        assert "shortfall-notice" in html_path.read_text().lower()

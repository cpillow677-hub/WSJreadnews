# WSJ Digest — Setup Guide

A daily pipeline that collects WSJ articles via RSS, scores and classifies them into four categories (Global, Market, Stock, Tech), generates factual summaries, and renders polished HTML and Markdown reports.

---

## Prerequisites

- Python **3.11+**
- A valid **WSJ account** (required for authenticated section scraping; RSS feeds work without login)
- `git` (to clone the repo)

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-org/WSJreadnews.git
cd WSJreadnews

# 2. (Recommended) Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

### Credentials

```bash
cp .env.example .env
```

Edit `.env` and set your WSJ credentials:

```
WSJ_EMAIL=your-email@example.com
WSJ_PASSWORD=your-password
```

> **Without credentials** the pipeline runs in RSS-only mode, which still collects the majority of stories. Section scraping (which requires login) is skipped automatically with a logged warning.

### Category tuning

Category definitions, keyword lists, RSS feeds, and scoring weights are all in `config/categories.yaml`. Edit this file to:

- Add or remove keywords for any category
- Adjust the `scoring` weights (`importance_weight`, `recency_weight`, `market_relevance_weight`)
- Change `dedup_threshold` (higher = stricter deduplication)
- Adjust `max_age_hours` to include older stories

---

## Running Locally

### Basic run (writes to `output/`)

```bash
python run_digest.py
```

### Dry run (no files written)

```bash
python run_digest.py --dry-run
```

### RSS-only mode (skip section scraping)

```bash
python run_digest.py --no-scrape
```

### Verbose logging

```bash
python run_digest.py --log-level DEBUG
```

### All options

```
python run_digest.py --help

Options:
  --config PATH         Path to categories.yaml  (default: config/categories.yaml)
  --output-dir PATH     Output directory          (default: output/)
  --date YYYY-MM-DD     Override date string      (default: today UTC)
  --dry-run             Run pipeline but skip writing output files
  --no-scrape           RSS-only mode (skip section scraping)
  --full-text           Fetch full article body for selected articles
  --log-level LEVEL     DEBUG|INFO|WARNING|ERROR  (default: INFO)
  --max-age-hours N     Override max_age_hours from config
```

---

## Output Files

After a successful run, two files are created in `output/`:

| File | Description |
|------|-------------|
| `output/daily_digest_YYYY-MM-DD.html` | Browser-ready HTML report with styled article cards |
| `output/daily_digest_YYYY-MM-DD.md`  | GitHub-renderable Markdown report |

Open the HTML file directly in any browser — no server required.

---

## Running Tests

```bash
pytest tests/test_pipeline.py -v
```

All tests mock HTTP calls — no network access or WSJ credentials required.

---

## GitHub Actions Setup

The pipeline runs automatically every day at **06:00 UTC** via `.github/workflows/daily_digest.yml`.

### Add secrets to your repository

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name   | Value                    |
|---------------|--------------------------|
| `WSJ_EMAIL`   | Your WSJ account email   |
| `WSJ_PASSWORD`| Your WSJ account password|

### Manual trigger

Go to **Actions → WSJ Daily Digest → Run workflow** to trigger a run manually. You can override the log level and enable dry-run mode from the UI.

### Artifacts

Each run uploads the output files as a GitHub Actions artifact named `daily-digest-YYYY-MM-DD`, retained for 30 days. Download them from the Actions run page.

### Auto-commit (scheduled runs)

On scheduled runs, the workflow automatically commits new digest files back to the repository with the message `digest: YYYY-MM-DD`. Disable this by removing the last step in `.github/workflows/daily_digest.yml`.

---

## Full-Text Mode with Playwright

WSJ increasingly renders articles with JavaScript. The `--full-text` flag enriches
selected articles with their complete body text before summarising, producing richer,
more specific summaries.

### How it works

1. After the 12 articles are selected, the pipeline fetches each article page.
2. With Playwright: a real Chromium browser logs in to WSJ using your credentials,
   executes JavaScript, and extracts the fully-rendered article text.
3. The summariser then uses extractive sentence scoring on the rich content.
4. Full text is **never reproduced** in output — summaries remain 100–150 words.

### Install Playwright

```bash
pip install playwright
playwright install chromium
```

### Run with full-text mode

```bash
# Playwright mode (recommended — handles JS-rendered pages)
USE_PLAYWRIGHT=1 python run_digest.py --full-text

# Or set permanently in config/categories.yaml:
#   use_full_text: true
#   use_playwright_fulltext: true

# requests mode (simpler, may get partial content on JS-heavy pages)
python run_digest.py --full-text
```

### Troubleshooting

| Issue | Fix |
|-------|-----|
| Login loop or blank page | Set `playwright_headless: false` in `config/categories.yaml` to watch the browser |
| "Playwright not installed" | Run `pip install playwright && playwright install chromium` |
| Empty full_text in logs | WSJ page may require JS; enable `use_playwright_fulltext: true` |
| Slow runs | Each article fetch takes ~5–10s. 12 articles ≈ 1–2 minutes extra |
| CAPTCHA challenge | Log in manually with `playwright_headless: false` on first run |

---

## Optional: Playwright Browser Automation (legacy flag)

For JavaScript-heavy pages that require full browser rendering:

```bash
pip install playwright==1.45.0
playwright install chromium

# Enable in your .env:
USE_PLAYWRIGHT=1

python run_digest.py
```

> **Note**: `PlaywrightFetcher` is currently a stub. Implement custom browser-based scraping logic in `wsj_digest/fetcher.py` → `PlaywrightFetcher.fetch()`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `403 Forbidden` on RSS feed | WSJ temporarily blocking the bot | Wait a few minutes and retry |
| `SHORTFALL: Category X has 0/3` | No matching articles in pool | Lower `dedup_threshold`, increase `max_age_hours`, or add keywords |
| `WSJ login failed (HTTP 401)` | Wrong credentials | Check `WSJ_EMAIL` / `WSJ_PASSWORD` in `.env` |
| `FileNotFoundError: config/categories.yaml` | Wrong working directory | Run from repo root: `cd WSJreadnews && python run_digest.py` |
| `ModuleNotFoundError: wsj_digest` | Not in repo root | Run `python run_digest.py` from the repository root directory |

---

## Project Structure

```
WSJreadnews/
├── .github/workflows/daily_digest.yml  # CI/CD: daily cron + manual trigger
├── config/categories.yaml              # Category defs, keywords, scoring weights
├── wsj_digest/
│   ├── models.py      # Article dataclass
│   ├── fetcher.py     # RSS + section scraping
│   ├── scorer.py      # Importance / recency / market-relevance scoring
│   ├── selector.py    # Deduplication + top-N selection
│   ├── summarizer.py  # Summary + why-it-matters generation
│   └── renderer.py    # HTML + Markdown output
├── output/            # Generated digest files (gitignored except .gitkeep)
├── sample_output/     # Example output with mock data
├── tests/             # pytest test suite (no network required)
├── run_digest.py      # CLI entrypoint
├── requirements.txt
└── SETUP.md           # This file
```

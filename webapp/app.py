"""
webapp/app.py — Flask web interface for Daily Financial Digest settings.

Allows users to configure:
  - Portfolio watchlist (stock tickers)
  - Output language (English, 繁體中文, 日本語, 한국어, Español)

Settings are stored in config/user_settings.json and optionally synced
to the GitHub repo via GitHub API (requires GITHUB_TOKEN env var).

Local dev:
    python webapp/app.py          → http://localhost:5001

Render deploy:
    Procfile: web: gunicorn -w 2 -b 0.0.0.0:$PORT webapp.app:app
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path

import requests as req
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ------------------------------------------------------------------ #
# Paths and constants
# ------------------------------------------------------------------ #
BASE_DIR = Path(__file__).parent.parent
SETTINGS_PATH = BASE_DIR / "config" / "user_settings.json"

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en":    "English",
    "zh-TW": "繁體中文",
    "ja":    "日本語",
    "ko":    "한국어",
    "es":    "Español",
}

DEFAULT_SETTINGS: dict = {
    "portfolio_tickers": ["ORCL", "QUBT", "AVGO", "ETN", "HON", "GLW", "AXTI"],
    "language": "en",
    "updated_at": None,
}

# GitHub API sync (optional — set env vars to enable)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "cpillow677-hub/WSJreadnews")
SETTINGS_GITHUB_PATH = "config/user_settings.json"


# ------------------------------------------------------------------ #
# Settings helpers
# ------------------------------------------------------------------ #

def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            with SETTINGS_PATH.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def _save_local(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _push_to_github(data: dict) -> bool:
    """Commit user_settings.json to the GitHub repo. Returns True on success."""
    if not GITHUB_TOKEN:
        return False
    owner, repo = GITHUB_REPO.split("/", 1)
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/contents/{SETTINGS_GITHUB_PATH}"
    )
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    # Fetch current SHA (needed for update)
    sha = None
    r = req.get(api_url, headers=headers, timeout=10)
    if r.ok:
        sha = r.json().get("sha")

    content_b64 = base64.b64encode(
        json.dumps(data, indent=2, ensure_ascii=False).encode()
    ).decode()
    payload: dict = {
        "message": (
            f"settings: update portfolio + language "
            f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC]"
        ),
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    r = req.put(api_url, json=payload, headers=headers, timeout=10)
    return r.ok


def save_settings(tickers: list[str], language: str) -> dict:
    data = {
        "portfolio_tickers": tickers,
        "language": language,
        "updated_at": datetime.utcnow().isoformat(),
    }
    _save_local(data)
    github_synced = _push_to_github(data)
    return {"local": True, "github": github_synced}


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    s = load_settings()
    return render_template(
        "index.html",
        tickers=s.get("portfolio_tickers", DEFAULT_SETTINGS["portfolio_tickers"]),
        language=s.get("language", "en"),
        updated_at=s.get("updated_at"),
        languages=SUPPORTED_LANGUAGES,
        github_enabled=bool(GITHUB_TOKEN),
    )


@app.route("/api/settings", methods=["GET"])
def api_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_save():
    body = request.get_json(force=True, silent=True) or {}
    raw_tickers = body.get("portfolio_tickers", [])
    language    = body.get("language", "en")

    # Validate
    tickers = [t.strip().upper() for t in raw_tickers if str(t).strip()]
    if not tickers:
        return jsonify({"error": "portfolio_tickers cannot be empty"}), 400
    if language not in SUPPORTED_LANGUAGES:
        return jsonify({"error": f"unsupported language: {language}"}), 400

    result = save_settings(tickers, language)
    return jsonify({
        "status": "ok",
        "portfolio_tickers": tickers,
        "language": language,
        "language_name": SUPPORTED_LANGUAGES[language],
        "github_synced": result["github"],
    })


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    app.run(debug=True, port=5001)

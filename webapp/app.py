"""
webapp/app.py — Flask web interface for Daily Financial Digest.

Features:
  - Settings page: configure portfolio tickers + output language
  - Immediate digest generation on save (background subprocess)
  - /digest: view the latest generated digest inline
  - /api/push-digest: authenticated endpoint for GitHub Actions to push
    the scheduled digest HTML directly to the webapp

Local dev:
    python webapp/app.py          → http://localhost:5001

Render deploy:
    Procfile: web: gunicorn -w 2 -b 0.0.0.0:$PORT webapp.app:app

Environment variables:
    GITHUB_TOKEN    — optional, sync settings to GitHub repo
    GITHUB_REPO     — e.g. cpillow677-hub/WSJreadnews
    PUSH_TOKEN      — secret token for /api/push-digest (same value in
                      GitHub Actions secret PUSH_TOKEN)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests as req
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Paths and constants
# ------------------------------------------------------------------ #
BASE_DIR      = Path(__file__).parent.parent
SETTINGS_PATH = BASE_DIR / "config" / "user_settings.json"
DIGEST_DIR    = Path("/tmp/wsj_digest_webapp")
STATUS_FILE   = DIGEST_DIR / "status.json"
DIGEST_FILE   = DIGEST_DIR / "latest.html"

DIGEST_DIR.mkdir(parents=True, exist_ok=True)

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

GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO          = os.environ.get("GITHUB_REPO", "cpillow677-hub/WSJreadnews")
SETTINGS_GITHUB_PATH = "config/user_settings.json"
PUSH_TOKEN           = os.environ.get("PUSH_TOKEN", "")

# Guard against concurrent pipeline runs
_pipeline_lock = threading.Lock()


# ------------------------------------------------------------------ #
# Digest status helpers (file-based so all gunicorn workers share it) #
# ------------------------------------------------------------------ #

def _read_status() -> dict:
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"status": "none", "timestamp": None, "error": None}


def _write_status(status: str, *, error: str = "") -> None:
    STATUS_FILE.write_text(
        json.dumps({
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": error,
        }),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ #
# Background pipeline runner
# ------------------------------------------------------------------ #

def _run_pipeline_bg() -> None:
    """
    Spawns run_digest.py as a subprocess, waits for it to finish,
    then stores the generated HTML in DIGEST_FILE.
    Designed to run in a daemon thread.
    """
    if not _pipeline_lock.acquire(blocking=False):
        logger.info("Pipeline already running — skipping duplicate trigger")
        return

    try:
        _write_status("running")
        logger.info("Background pipeline started")

        output_dir = DIGEST_DIR / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                sys.executable,
                str(BASE_DIR / "run_digest.py"),
                "--output-dir", str(output_dir),
                "--no-scrape",          # RSS-only on Render (no WSJ login)
                "--log-level", "INFO",
            ],
            capture_output=True,
            text=True,
            timeout=360,
            cwd=str(BASE_DIR),
            env={**os.environ},
        )

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")[-3000:]
            logger.error("Pipeline subprocess failed:\n%s", err)
            _write_status("error", error=err)
            return

        html_files = sorted(output_dir.glob("daily_digest_*.html"))
        if not html_files:
            _write_status("error", error="Pipeline succeeded but no HTML file was produced.")
            return

        DIGEST_FILE.write_bytes(html_files[-1].read_bytes())
        _write_status("ready")
        logger.info("Pipeline complete — digest available at /digest")

    except subprocess.TimeoutExpired:
        _write_status("error", error="Pipeline timed out after 6 minutes.")
    except Exception as exc:
        _write_status("error", error=str(exc))
        logger.exception("Pipeline background task raised an exception")
    finally:
        _pipeline_lock.release()


def _trigger_pipeline() -> None:
    t = threading.Thread(target=_run_pipeline_bg, daemon=True)
    t.start()


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
    st = _read_status()
    return render_template(
        "index.html",
        tickers=s.get("portfolio_tickers", DEFAULT_SETTINGS["portfolio_tickers"]),
        language=s.get("language", "en"),
        updated_at=s.get("updated_at"),
        languages=SUPPORTED_LANGUAGES,
        github_enabled=bool(GITHUB_TOKEN),
        digest_status=st["status"],
        digest_timestamp=st.get("timestamp"),
    )


@app.route("/digest")
def view_digest():
    st = _read_status()
    if st["status"] == "ready" and DIGEST_FILE.exists():
        html = DIGEST_FILE.read_text(encoding="utf-8")
        # Inject a small settings bar at the top of the generated digest
        settings_bar = (
            '<div style="background:#003366;color:#fff;padding:0.6rem 1.5rem;'
            'font-family:Arial,sans-serif;font-size:0.85rem;display:flex;'
            'justify-content:space-between;align-items:center;">'
            '<span>Daily Financial Digest</span>'
            '<a href="/" style="color:#ffd700;text-decoration:none;">⚙ Settings</a>'
            '</div>'
        )
        html = html.replace("<body>", f"<body>{settings_bar}", 1)
        return Response(html, mimetype="text/html; charset=utf-8")

    status = st["status"]
    timestamp = st.get("timestamp", "")
    error = st.get("error", "")
    return render_template(
        "digest_status.html",
        status=status,
        timestamp=timestamp,
        error=error,
    )


@app.route("/api/settings", methods=["GET"])
def api_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_save():
    body = request.get_json(force=True, silent=True) or {}
    raw_tickers = body.get("portfolio_tickers", [])
    language    = body.get("language", "en")

    tickers = [t.strip().upper() for t in raw_tickers if str(t).strip()]
    if not tickers:
        return jsonify({"error": "portfolio_tickers cannot be empty"}), 400
    if language not in SUPPORTED_LANGUAGES:
        return jsonify({"error": f"unsupported language: {language}"}), 400

    result = save_settings(tickers, language)
    _trigger_pipeline()

    return jsonify({
        "status": "ok",
        "portfolio_tickers": tickers,
        "language": language,
        "language_name": SUPPORTED_LANGUAGES[language],
        "github_synced": result["github"],
        "digest_generating": True,
    })


@app.route("/api/digest-status", methods=["GET"])
def api_digest_status():
    return jsonify(_read_status())


@app.route("/api/push-digest", methods=["POST"])
def api_push_digest():
    """
    Called by GitHub Actions after a scheduled digest run.
    Expects:
      Authorization: Bearer <PUSH_TOKEN>
      Content-Type: text/html
      Body: the generated HTML content
    """
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not PUSH_TOKEN or token != PUSH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    html = request.get_data(as_text=True)
    if not html or len(html) < 100:
        return jsonify({"error": "empty or too-short HTML body"}), 400

    DIGEST_FILE.write_text(html, encoding="utf-8")
    _write_status("ready")
    logger.info("Digest pushed from GitHub Actions (%d bytes)", len(html))
    return jsonify({"status": "ok", "bytes": len(html)})


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    app.run(debug=True, port=5001)

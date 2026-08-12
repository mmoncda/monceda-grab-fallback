import os
import re
import subprocess
from urllib.parse import urlparse
from flask import Flask, jsonify, request

app = Flask(__name__)

URL_RE = re.compile(r"^https?://", re.I)


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "monceda-grab-fallback",
        "engine": "yt-dlp"
    })


@app.post("/extract")
def extract():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url or not URL_RE.match(url):
        return jsonify({
            "status": "error",
            "error": "invalid_url"
        }), 400

    # Current fallback scope: public Instagram URLs only.
    try:
        host = re.sub(r"^www\.", "", urlparse(url).hostname or "")
    except Exception:
        host = ""

    if host not in {"instagram.com"}:
        return jsonify({
            "status": "error",
            "error": "unsupported_host"
        }), 400

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-download",
        "--no-warnings",
        "--print", "%(id)s",
        "--print", "%(ext)s",
        "--print", "%(url)s",
        url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return jsonify({
            "status": "error",
            "error": "extract_timeout"
        }), 504

    if result.returncode != 0:
        return jsonify({
            "status": "error",
            "error": "extract_failed",
            "detail": result.stderr[-1000:]
        }), 422

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    if len(lines) < 3:
        return jsonify({
            "status": "error",
            "error": "no_media"
        }), 422

    media_id, ext, media_url = lines[-3:]

    if not media_url.startswith(("http://", "https://")):
        return jsonify({
            "status": "error",
            "error": "invalid_media_url"
        }), 422

    return jsonify({
        "status": "ok",
        "engine": "yt-dlp",
        "id": media_id,
        "ext": ext,
        "filename": f"instagram_{media_id}.{ext}",
        "url": media_url
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

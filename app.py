import json
import os
import re
import subprocess
from urllib.parse import urlparse

from flask import Flask, jsonify, request

app = Flask(__name__)

URL_RE = re.compile(r"^https?://", re.I)


def is_http_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def choose_media(info):
    # yt-dlp's selected media URL is normally here.
    if is_http_url(info.get("url")):
        return info.get("url"), info.get("ext") or "mp4"

    # Some extractors return selected formats separately.
    requested = info.get("requested_formats") or []
    for item in requested:
        if is_http_url(item.get("url")) and item.get("vcodec") not in (None, "none"):
            return item["url"], item.get("ext") or "mp4"

    # Final fallback: choose the best video-bearing format.
    formats = info.get("formats") or []
    candidates = [
        item for item in formats
        if is_http_url(item.get("url"))
        and item.get("vcodec") not in (None, "none")
    ]

    if candidates:
        candidates.sort(
            key=lambda item: (
                item.get("acodec") not in (None, "none"),
                item.get("height") or 0,
                item.get("tbr") or 0,
            ),
            reverse=True,
        )
        selected = candidates[0]
        return selected["url"], selected.get("ext") or "mp4"

    return None, None


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "monceda-grab-fallback",
        "engine": "yt-dlp",
    })


@app.get("/debug/impersonation")
def debug_impersonation():
    result = subprocess.run(
        ["yt-dlp", "--list-impersonate-targets"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    return jsonify({
        "status": "ok" if result.returncode == 0 else "error",
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })


@app.post("/extract")
def extract():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url or not URL_RE.match(url):
        return jsonify({
            "status": "error",
            "error": "invalid_url",
        }), 400

    try:
        host = re.sub(r"^www\.", "", urlparse(url).hostname or "")
    except Exception:
        host = ""

    supported_hosts = {
        "instagram.com",
        "bsky.app",
        "dailymotion.com",
        "dai.ly",
        "vimeo.com",
        "player.vimeo.com",
    }

    if host not in supported_hosts:
        return jsonify({
            "status": "error",
            "error": "unsupported_host",
        }), 400

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-download",
        "--no-warnings",
        "--dump-single-json",
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
            "error": "extract_timeout",
        }), 504

    if result.returncode != 0:
        return jsonify({
            "status": "error",
            "error": "extract_failed",
            "detail": result.stderr[-1000:],
        }), 422

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return jsonify({
            "status": "error",
            "error": "invalid_extractor_response",
        }), 502

    media_url, ext = choose_media(info)

    if not media_url:
        return jsonify({
            "status": "error",
            "error": "no_video_media",
        }), 422

    media_id = str(info.get("id") or "media")

    return jsonify({
        "status": "ok",
        "engine": "yt-dlp",
        "id": media_id,
        "ext": ext,
        "filename": f"{host.replace('.', '_')}_{media_id}.{ext}",
        "url": media_url,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

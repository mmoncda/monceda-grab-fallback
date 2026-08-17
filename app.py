import json
import os
import re
import subprocess
import tempfile
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, after_this_request

app = Flask(__name__)

URL_RE = re.compile(r"^https?://", re.I)


def is_http_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def is_h264_codec(value):
    codec = str(value or "").lower()
    return (
        codec.startswith("avc1")
        or codec.startswith("avc")
        or "h264" in codec
        or "h.264" in codec
    )


def choose_media(info):
    """
    Prefer a broadly compatible H.264/AVC MP4 rendition when yt-dlp
    exposes one. Fall back to the extractor's selected URL only when
    no better compatible option is available.
    """

    formats = info.get("formats") or []

    video_candidates = [
        item for item in formats
        if is_http_url(item.get("url"))
        and item.get("vcodec") not in (None, "none")
    ]

    if video_candidates:
        def score(item):
            ext = str(item.get("ext") or "").lower()
            vcodec = str(item.get("vcodec") or "").lower()
            acodec = item.get("acodec")

            has_audio = acodec not in (None, "none")
            is_mp4 = ext in ("mp4", "m4v", "mov")
            h264 = is_h264_codec(vcodec)

            return (
                h264,
                is_mp4,
                has_audio,
                item.get("height") or 0,
                item.get("tbr") or 0,
            )

        video_candidates.sort(
            key=score,
            reverse=True,
        )

        selected = video_candidates[0]

        return (
            selected["url"],
            selected.get("ext") or "mp4",
            selected.get("vcodec"),
            selected.get("acodec"),
        )

    requested = info.get("requested_formats") or []

    requested_video = [
        item for item in requested
        if is_http_url(item.get("url"))
        and item.get("vcodec") not in (None, "none")
    ]

    if requested_video:
        requested_video.sort(
            key=lambda item: (
                is_h264_codec(item.get("vcodec")),
                str(item.get("ext") or "").lower() == "mp4",
                item.get("acodec") not in (None, "none"),
                item.get("height") or 0,
                item.get("tbr") or 0,
            ),
            reverse=True,
        )

        selected = requested_video[0]

        return (
            selected["url"],
            selected.get("ext") or "mp4",
            selected.get("vcodec"),
            selected.get("acodec"),
        )

    if is_http_url(info.get("url")):
        return (
            info.get("url"),
            info.get("ext") or "mp4",
            info.get("vcodec"),
            info.get("acodec"),
        )

    return None, None, None, None


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "monceda-grab-fallback",
        "engine": "yt-dlp",
        "build": "normalized-h264-aac-v2",
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


@app.post("/debug/formats")
def debug_formats():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url or not URL_RE.match(url):
        return jsonify({
            "status": "error",
            "error": "invalid_url",
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

    formats = []

    for item in info.get("formats") or []:
        formats.append({
            "format_id": item.get("format_id"),
            "ext": item.get("ext"),
            "vcodec": item.get("vcodec"),
            "acodec": item.get("acodec"),
            "width": item.get("width"),
            "height": item.get("height"),
            "fps": item.get("fps"),
            "tbr": item.get("tbr"),
            "protocol": item.get("protocol"),
            "has_url": is_http_url(item.get("url")),
        })

    return jsonify({
        "status": "ok",
        "id": info.get("id"),
        "selected": {
            "format_id": info.get("format_id"),
            "ext": info.get("ext"),
            "vcodec": info.get("vcodec"),
            "acodec": info.get("acodec"),
        },
        "formats": formats,
    })


@app.post("/download")
def download_media():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url or not URL_RE.match(url):
        return jsonify({
            "status": "error",
            "error": "invalid_url",
        }), 400

    try:
        host = re.sub(
            r"^www\.",
            "",
            urlparse(url).hostname or "",
        )
    except Exception:
        host = ""

    supported_hosts = {
        "instagram.com",
        "bsky.app",
        "dailymotion.com",
        "dai.ly",
        "vimeo.com",
        "player.vimeo.com",
        "bilibili.tv",
    }

    if host not in supported_hosts:
        return jsonify({
            "status": "error",
            "error": "unsupported_host",
        }), 400

    try:
        with tempfile.TemporaryDirectory(
            prefix="monceda-grab-"
        ) as temp_dir:
            output = os.path.join(
                temp_dir,
                "media.mp4",
            )

            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--no-warnings",

                # Prefer a directly compatible H.264/AAC stream.
                # If unavailable, yt-dlp/ffmpeg obtains the best
                # available streams and converts the final file.
                "-f",
                (
                    "best[vcodec^=avc1][acodec^=mp4a]/"
                    "bestvideo[vcodec^=avc1]+"
                    "bestaudio[acodec^=mp4a]/"
                    "bestvideo+bestaudio/best"
                ),

                "--merge-output-format",
                "mp4",
                "-o",
                output,
                url,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

            if (
                result.returncode != 0
                or not os.path.isfile(output)
            ):
                return jsonify({
                    "status": "error",
                    "error": "download_failed",
                    "detail": result.stderr[-1200:],
                }), 422

            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=nw=1:nk=1",
                    output,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            video_codec = probe.stdout.strip().lower()

            audio_probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=nw=1:nk=1",
                    output,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            audio_codec = (
                audio_probe.stdout.strip().lower()
            )

            final_path = output

            if (
                video_codec != "h264"
                or (
                    audio_codec
                    and audio_codec != "aac"
                )
            ):
                normalized = os.path.join(
                    temp_dir,
                    "normalized.mp4",
                )

                ffmpeg = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        output,
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "21",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "160k",
                        "-movflags",
                        "+faststart",
                        normalized,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )

                if (
                    ffmpeg.returncode != 0
                    or not os.path.isfile(normalized)
                ):
                    return jsonify({
                        "status": "error",
                        "error": "normalize_failed",
                        "detail": ffmpeg.stderr[-1200:],
                    }), 500

                final_path = normalized

            #
            # Move the final media outside TemporaryDirectory so Flask
            # can stream it from disk instead of loading the entire
            # video into the Render worker's memory.
            #
            import shutil

            fd, response_path = tempfile.mkstemp(
                prefix="monceda-grab-response-",
                suffix=".mp4",
            )
            os.close(fd)

            shutil.move(final_path, response_path)

            @after_this_request
            def cleanup_response_file(response):
                try:
                    os.remove(response_path)
                except OSError:
                    pass
                return response

            response = send_file(
                response_path,
                mimetype="video/mp4",
                as_attachment=True,
                download_name="monceda-grab-media.mp4",
                conditional=False,
            )

            response.headers["Cache-Control"] = (
                "private, no-store"
            )
            response.headers[
                "X-Monceda-Normalized"
            ] = "h264-aac"

            return response

    except subprocess.TimeoutExpired:
        return jsonify({
            "status": "error",
            "error": "processing_timeout",
        }), 504


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
        "bilibili.tv",
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

    media_url, ext, vcodec, acodec = choose_media(info)

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
        "vcodec": vcodec,
        "acodec": acodec,
        "filename": f"{host.replace('.', '_')}_{media_id}.{ext}",
        "url": media_url,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

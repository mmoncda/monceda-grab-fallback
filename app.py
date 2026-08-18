import json
import os
import re
import subprocess
import tempfile
import shutil
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

URL_RE = re.compile(r"^https?://", re.I)

INSTAGRAM_COOKIE_SECRET_PATH = os.environ.get(
    "INSTAGRAM_COOKIE_SECRET_PATH",
    "/secrets/instagram/cookies.txt",
)


def is_instagram_story_url(value):
    try:
        parsed = urlparse(value)
        host = re.sub(
            r"^www\.",
            "",
            (parsed.hostname or "").lower(),
        )

        return (
            host == "instagram.com"
            and parsed.path.startswith("/stories/")
        )
    except Exception:
        return False


def copy_instagram_cookie_file(destination_dir=None):
    """
    Cloud Run Secret Manager mounts are read-only.

    yt-dlp may update a Netscape cookie jar, so copy the
    mounted Instagram-only secret into a writable temporary
    file for each authenticated Story extraction.
    """
    source = INSTAGRAM_COOKIE_SECRET_PATH

    if not os.path.isfile(source):
        return None

    fd, temp_path = tempfile.mkstemp(
        prefix="monceda-instagram-cookies-",
        suffix=".txt",
        dir=destination_dir,
    )

    os.close(fd)

    shutil.copyfile(source, temp_path)
    os.chmod(temp_path, 0o600)

    return temp_path


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


def choose_audio(info):
    # First prefer an explicitly requested audio stream.
    requested = info.get("requested_formats") or []

    audio_candidates = [
        item for item in requested
        if is_http_url(item.get("url"))
        and item.get("acodec") not in (None, "none")
        and item.get("vcodec") in (None, "none")
    ]

    # Otherwise inspect all available formats.
    if not audio_candidates:
        formats = info.get("formats") or []

        audio_candidates = [
            item for item in formats
            if is_http_url(item.get("url"))
            and item.get("acodec") not in (None, "none")
            and item.get("vcodec") in (None, "none")
        ]

    if not audio_candidates:
        return None

    audio_candidates.sort(
        key=lambda item: (
            item.get("abr") or 0,
            item.get("tbr") or 0,
        ),
        reverse=True,
    )

    return audio_candidates[0]["url"]


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "monceda-grab-fallback",
        "engine": "yt-dlp",
        "build": "bilibili-tv-454bcf1",
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


@app.post("/instagram/download")
def instagram_download():
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

    if host != "instagram.com":
        return jsonify({
            "status": "error",
            "error": "unsupported_host",
        }), 400

    temp_dir = tempfile.mkdtemp(
        prefix="monceda-instagram-"
    )

    output_template = os.path.join(
        temp_dir,
        "media.%(ext)s",
    )

    try:
        #
        # IMPORTANT:
        # No transcoding here.
        #
        # Select H.264 video + AAC/M4A audio and let
        # yt-dlp/FFmpeg MERGE them into one MP4.
        #
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "-f",
            (
                "bestvideo[vcodec^=avc1]+"
                "bestaudio[acodec^=mp4a]/"
                "bestvideo[vcodec^=avc1]+"
                "bestaudio[ext=m4a]/"
                "best[ext=mp4][vcodec^=avc1]/"
                "bestvideo+bestaudio/best"
            ),
            "--merge-output-format",
            "mp4",
            "-o",
            output_template,
            url,
        ]

        if is_instagram_story_url(url):
            cookie_path = copy_instagram_cookie_file(
                destination_dir=temp_dir
            )

            if not cookie_path:
                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True,
                )

                return jsonify({
                    "status": "error",
                    "error": "instagram_story_auth_unavailable",
                }), 503

            # Insert before the final URL argument.
            cmd[-1:-1] = [
                "--cookies",
                cookie_path,
            ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        if result.returncode != 0:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )

            return jsonify({
                "status": "error",
                "error": "instagram_download_failed",
                "detail": result.stderr[-1200:],
            }), 422

        candidates = []

        for name in os.listdir(temp_dir):
            path = os.path.join(temp_dir, name)

            if (
                os.path.isfile(path)
                and name.lower().endswith(".mp4")
            ):
                candidates.append(path)

        if not candidates:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )

            return jsonify({
                "status": "error",
                "error": "instagram_mp4_missing",
            }), 422

        final_path = max(
            candidates,
            key=os.path.getsize,
        )

        # Instagram Stories must be normalized for Apple/QuickTime.
        # A file being .mp4 is not enough: force H.264 + AAC,
        # yuv420p and a standard non-fragmented MP4 layout.
        if is_instagram_story_url(url):
            compatible_path = os.path.join(
                temp_dir,
                "instagram-story-compatible.mp4",
            )

            transcode_cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                final_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-profile:v",
                "high",
                "-level",
                "4.1",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                compatible_path,
            ]

            transcode_result = subprocess.run(
                transcode_cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

            if (
                transcode_result.returncode != 0
                or not os.path.isfile(compatible_path)
                or os.path.getsize(compatible_path) == 0
            ):
                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True,
                )

                return jsonify({
                    "status": "error",
                    "error": "instagram_story_transcode_failed",
                    "detail": transcode_result.stderr[-1200:],
                }), 422

            final_path = compatible_path

        response = send_file(
            final_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="instagram-video.mp4",
            conditional=False,
        )

        response.headers["Cache-Control"] = (
            "private, no-store"
        )
        response.headers[
            "X-Monceda-Instagram"
        ] = "h264-aac-merged"

        response.call_on_close(
            lambda: shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )
        )

        return response

    except subprocess.TimeoutExpired:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        return jsonify({
            "status": "error",
            "error": "instagram_download_timeout",
        }), 504

    except Exception as error:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        app.logger.exception(
            "Instagram merged download failed"
        )

        return jsonify({
            "status": "error",
            "error": "instagram_download_failed",
            "detail": str(error),
        }), 500


def is_instagram_media_url(value):
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme == "https"
            and (
                host == "fbcdn.net"
                or host.endswith(".fbcdn.net")
            )
        )
    except Exception:
        return False


@app.post("/instagram/normalize")
def instagram_normalize():
    data = request.get_json(silent=True) or {}
    media_url = str(data.get("url", "")).strip()
    audio_url = str(data.get("audio_url", "")).strip()

    if not is_instagram_media_url(media_url):
        return jsonify({
            "status": "error",
            "error": "invalid_media_url",
        }), 400

    if audio_url and not is_instagram_media_url(audio_url):
        return jsonify({
            "status": "error",
            "error": "invalid_audio_url",
        }), 400

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "2",
        "-i",
        media_url,
    ]

    if audio_url:
        cmd += [
            "-i",
            audio_url,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    else:
        cmd += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
        ]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "superfast",
        "-tune",
        "zerolatency",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "2",

        "-c:a",
        "aac",
        "-b:a",
        "96k",

        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    def generate():
        try:
            while True:
                chunk = process.stdout.read(256 * 1024)

                if not chunk:
                    break

                yield chunk
        finally:
            if process.stdout:
                process.stdout.close()

            if process.poll() is None:
                process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    response = Response(
        generate(),
        mimetype="video/mp4",
        direct_passthrough=True,
    )

    response.headers["Content-Disposition"] = (
        'attachment; filename="instagram-video.mp4"'
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Monceda-Instagram"] = "h264-stream"

    return response


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
        "tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
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

    cookie_path = None

    if is_instagram_story_url(url):
        cookie_path = copy_instagram_cookie_file()

        if not cookie_path:
            return jsonify({
                "status": "error",
                "error": "instagram_story_auth_unavailable",
            }), 503

        cmd[-1:-1] = [
            "--cookies",
            cookie_path,
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
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass

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

    audio_url = (
        choose_audio(info)
        if host == "instagram.com"
        else None
    )

    response = {
        "status": "ok",
        "engine": "yt-dlp",
        "id": media_id,
        "ext": ext,
        "filename": f"{host.replace('.', '_')}_{media_id}.{ext}",
        "url": media_url,
        "title": str(info.get("title") or "").strip(),
        "author": str(
            info.get("uploader")
            or info.get("channel")
            or info.get("creator")
            or ""
        ).strip(),
        "duration": info.get("duration"),
        "upload_date": str(info.get("upload_date") or "").strip(),
    }

    if audio_url:
        response["audio_url"] = audio_url

    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

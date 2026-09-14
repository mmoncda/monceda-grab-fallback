import hmac
import json
import os
import re
import subprocess
import tempfile
import shutil
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

PROCESSOR_TOKEN_ENV = "MONCEDA_PROCESSOR_TOKEN"
PROCESSOR_TOKEN_HEADER = "X-Monceda-Processor-Token"

PROTECTED_PROCESSOR_ENDPOINTS = frozenset({
    "instagram_download",
    "instagram_story_extract",
    "instagram_story_download",
    "instagram_normalize",
    "extract",
    "facebook_story_extract",
})


@app.before_request
def enforce_processor_auth():
    """
    Restrict productive processor routes to trusted server-side callers.

    Health remains public. Production-disabled debug routes remain 404
    and are intentionally excluded from this authentication boundary.
    """
    endpoint = request.endpoint or ""

    if endpoint not in PROTECTED_PROCESSOR_ENDPOINTS:
        return None

    expected_token = os.environ.get(
        PROCESSOR_TOKEN_ENV,
        "",
    ).strip()

    if not expected_token:
        app.logger.error(
            "Processor authentication token is not configured"
        )

        return jsonify({
            "status": "error",
            "error": "service_unavailable",
        }), 503

    provided_token = request.headers.get(
        PROCESSOR_TOKEN_HEADER,
        "",
    )

    if (
        not provided_token
        or not hmac.compare_digest(
            provided_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        )
    ):
        return jsonify({
            "status": "error",
            "error": "unauthorized",
        }), 401

    return None



URL_RE = re.compile(r"^https?://", re.I)


def is_instagram_story_url(value):
    try:
        parsed = urlparse(value)
        host = re.sub(
            r"^www\.",
            "",
            (parsed.hostname or "").lower(),
        )

        return (
            parsed.scheme == "https"
            and host == "instagram.com"
            and re.match(
                r"^/stories/[^/]+(?:/\d+)?/?$",
                parsed.path,
                re.I,
            )
            is not None
        )
    except Exception:
        return False


INSTAGRAM_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789-_"
)


def instagram_media_id_to_shortcode(value):
    text = str(value or "").strip()

    if not text.isdigit():
        return ""

    number = int(text)

    if number <= 0:
        return ""

    result = ""

    while number:
        number, remainder = divmod(number, 64)
        result = (
            INSTAGRAM_SHORTCODE_ALPHABET[remainder]
            + result
        )

    return result


def select_instagram_story_info(info, url):
    if not isinstance(info, dict):
        return None

    entries = info.get("entries")

    if not isinstance(entries, list):
        return info

    story_id = ""

    try:
        parts = [
            item
            for item in urlparse(url).path.split("/")
            if item
        ]

        if len(parts) >= 3:
            story_id = parts[2]
    except Exception:
        story_id = ""

    if story_id:
        story_shortcode = (
            instagram_media_id_to_shortcode(story_id)
        )

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            entry_id = str(entry.get("id") or "")

            if (
                entry_id == story_id
                or (
                    story_shortcode
                    and entry_id == story_shortcode
                )
            ):
                return entry

    for entry in entries:
        if isinstance(entry, dict):
            return entry

    return None


def extract_instagram_story_info(url):
    """
    Extract only Stories that Instagram exposes without using
    Monceda Grab's authenticated account session.

    If Instagram requires authentication, fail closed instead
    of expanding the caller's access through server credentials.
    """
    cmd = [
        "yt-dlp",
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
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            "instagram_story_extract_timeout",
            "",
            504,
        )

    if result.returncode != 0:
        return (
            None,
            "instagram_story_public_unavailable",
            "",
            422,
        )

    try:
        root_info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return (
            None,
            "instagram_story_invalid_response",
            "",
            502,
        )

    root_entries = (
        root_info.get("entries")
        if isinstance(root_info, dict)
        else None
    )

    info = select_instagram_story_info(
        root_info,
        url,
    )

    if isinstance(info, dict):
        info["_monceda_root_entries"] = (
            root_entries
            if isinstance(root_entries, list)
            else []
        )

    if not isinstance(info, dict):
        return (
            None,
            "instagram_story_public_unavailable",
            "",
            422,
        )

    return info, None, "", 200


INSTAGRAM_ID_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789-_"
)


def instagram_pk_to_id(media_id):
    try:
        value = int(str(media_id).split("_", 1)[0])
    except (TypeError, ValueError):
        return ""

    if value == 0:
        return INSTAGRAM_ID_CHARS[0]

    encoded = []

    while value:
        value, remainder = divmod(
            value,
            len(INSTAGRAM_ID_CHARS),
        )
        encoded.append(INSTAGRAM_ID_CHARS[remainder])

    return "".join(reversed(encoded))


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

    # Photo Stories may not expose a video-bearing format.
    image_exts = {"jpg", "jpeg", "png", "webp", "gif", "avif"}

    image_candidates = [
        item for item in formats
        if isinstance(item, dict)
        and is_http_url(item.get("url"))
        and str(item.get("ext") or "").lower() in image_exts
    ]

    if image_candidates:
        image_candidates.sort(
            key=lambda item: (
                item.get("width") or 0,
                item.get("height") or 0,
            ),
            reverse=True,
        )
        selected = image_candidates[0]
        return (
            selected["url"],
            str(selected.get("ext") or "jpg").lower(),
        )

    thumbnail = info.get("thumbnail")

    if is_http_url(thumbnail):
        clean_url = thumbnail.split("?", 1)[0].lower()

        for image_ext in image_exts:
            if clean_url.endswith(f".{image_ext}"):
                return thumbnail, image_ext

        return thumbnail, "jpg"

    thumbnails = info.get("thumbnails") or []

    for item in reversed(thumbnails):
        if not isinstance(item, dict):
            continue

        image_url = item.get("url")

        if not is_http_url(image_url):
            continue

        image_ext = str(item.get("ext") or "").lower()

        if image_ext not in image_exts:
            image_ext = "jpg"

        return image_url, image_ext

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
    """
    Production diagnostic endpoint intentionally disabled.
    """
    return jsonify({
        "status": "error",
        "error": "not_found",
    }), 404


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
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]"
            ),
            "--merge-output-format",
            "mp4",
            "-o",
            output_template,
            url,
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
                or host == "cdninstagram.com"
                or host.endswith(".cdninstagram.com")
            )
        )
    except Exception:
        return False


def fetch_instagram_raw_story_items(url):
    """
    Authenticated Instagram Story API enrichment is intentionally
    disabled for the public Monceda Grab service.

    Anonymous yt-dlp output remains the only Story source. This
    means some photo Stories may be unavailable rather than using
    Monceda Grab's account session to expand access.
    """
    return []


def choose_instagram_story_photo(raw_item):
    if not isinstance(raw_item, dict):
        return None, None

    if raw_item.get("media_type") != 1:
        return None, None

    candidates = (
        (
            raw_item.get("image_versions2")
            or {}
        ).get("candidates")
        or []
    )

    valid = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        image_url = candidate.get("url")

        if (
            not is_http_url(image_url)
            or not is_instagram_media_url(image_url)
        ):
            continue

        valid.append(candidate)

    if not valid:
        return None, None

    valid.sort(
        key=lambda candidate: (
            (candidate.get("width") or 0)
            * (candidate.get("height") or 0),
            candidate.get("width") or 0,
            candidate.get("height") or 0,
        ),
        reverse=True,
    )

    image_url = valid[0]["url"]

    clean_url = image_url.split("?", 1)[0].lower()

    image_ext = "jpg"

    for candidate_ext in (
        "jpeg",
        "jpg",
        "png",
        "webp",
        "gif",
        "avif",
    ):
        if clean_url.endswith(f".{candidate_ext}"):
            image_ext = candidate_ext
            break

    return image_url, image_ext


@app.post("/instagram/story/debug")

def instagram_story_debug():
    """
    Disabled in the public service because the previous diagnostic
    implementation used Monceda Grab's authenticated Instagram
    session.
    """
    return jsonify({
        "status": "error",
        "error": "not_found",
    }), 404


@app.post("/instagram/story/raw-debug")

def instagram_story_raw_debug():
    """
    Disabled in the public service because the previous diagnostic
    implementation used Monceda Grab's authenticated Instagram
    session.
    """
    return jsonify({
        "status": "error",
        "error": "not_found",
    }), 404


@app.post("/instagram/story/web-debug")

def instagram_story_web_debug():
    """
    Disabled in the public service because the previous diagnostic
    implementation used Monceda Grab's authenticated Instagram
    session.
    """
    return jsonify({
        "status": "error",
        "error": "not_found",
    }), 404


@app.post("/instagram/story/extract")
def instagram_story_extract():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_instagram_story_url(url):
        return jsonify({
            "status": "error",
            "error": "invalid_instagram_story_url",
        }), 400

    info, error, detail, status_code = (
        extract_instagram_story_info(url)
    )

    if error:
        return jsonify({
            "status": "error",
            "error": error,
        }), status_code

    media_url, ext = choose_media(info)
    audio_url = choose_audio(info)

    if (
        not media_url
        or not is_instagram_media_url(media_url)
    ):
        return jsonify({
            "status": "error",
            "error": "instagram_story_media_missing",
        }), 422

    if (
        audio_url
        and not is_instagram_media_url(audio_url)
    ):
        audio_url = None

    media_id = str(info.get("id") or "story")

    duration = info.get("duration")

    story_items = []

    root_entries = info.get("_monceda_root_entries")

    if not isinstance(root_entries, list):
        root_entries = []

    #
    # Preserve every yt-dlp Story entry first. Video extraction
    # continues to use the existing, already-working path.
    #
    yt_items_by_id = {}

    for index, entry in enumerate(root_entries):
        if not isinstance(entry, dict):
            continue

        item_media_url, item_ext = choose_media(entry)
        item_audio_url = choose_audio(entry)

        if (
            not item_media_url
            or not is_instagram_media_url(item_media_url)
        ):
            continue

        if (
            item_audio_url
            and not is_instagram_media_url(item_audio_url)
        ):
            item_audio_url = None

        item_id = str(
            entry.get("id")
            or f"story-{index + 1}"
        )

        item = {
            "id": item_id,
            "index": index + 1,
            "url": item_media_url,
            "ext": item_ext or "mp4",
            "filename": (
                f"instagram_story_{item_id}."
                f"{item_ext or 'mp4'}"
            ),
            "title": str(
                entry.get("title")
                or f"Instagram Story {index + 1}"
            ).strip(),
            "duration": entry.get("duration"),
        }

        if item_audio_url:
            item["audio_url"] = item_audio_url

        item_thumbnail = entry.get("thumbnail")

        if (
            isinstance(item_thumbnail, str)
            and item_thumbnail.startswith("https://")
        ):
            item["thumbnail"] = item_thumbnail

        yt_items_by_id[item_id] = item

    #
    # Instagram's raw Story response retains media_type.
    #
    # media_type == 1 -> original photo candidate
    # media_type == 2 -> existing yt-dlp video item
    #
    # Numeric PK is converted with Instagram's deterministic
    # base64-style shortcode algorithm, matching yt-dlp.
    #
    raw_items = fetch_instagram_raw_story_items(url)

    emitted_ids = set()

    for raw_index, raw_item in enumerate(raw_items, 1):
        raw_pk = raw_item.get("pk")
        raw_id = instagram_pk_to_id(raw_pk)

        if not raw_id:
            continue

        media_type = raw_item.get("media_type")

        if media_type == 1:
            photo_url, photo_ext = (
                choose_instagram_story_photo(raw_item)
            )

            if not photo_url:
                continue

            photo_item = {
                "id": raw_id,
                "index": len(story_items) + 1,
                "url": photo_url,
                "ext": photo_ext or "jpg",
                "filename": (
                    f"instagram_story_{raw_id}."
                    f"{photo_ext or 'jpg'}"
                ),
                "title": f"Instagram Story {raw_index}",
                "duration": None,
            }

            story_items.append(photo_item)
            emitted_ids.add(raw_id)
            continue

        existing = yt_items_by_id.get(raw_id)

        if existing:
            existing = dict(existing)
            existing["index"] = len(story_items) + 1
            story_items.append(existing)
            emitted_ids.add(raw_id)

    #
    # Fail-safe: if raw API enrichment is incomplete or unavailable,
    # append any yt-dlp items that were not represented above.
    #
    for item_id, item in yt_items_by_id.items():
        if item_id in emitted_ids:
            continue

        fallback_item = dict(item)
        fallback_item["index"] = len(story_items) + 1
        story_items.append(fallback_item)
        emitted_ids.add(item_id)

    # For username-level Story URLs, the root response should
    # represent the first Story in the ordered merged Story list.
    # Numeric Story URLs retain their existing selected-item behavior.
    has_numeric_story_id = re.search(
        r"/stories/[^/]+/\d+/?$",
        url,
        re.I,
    ) is not None

    if story_items and not has_numeric_story_id:
        first_story = story_items[0]

        media_id = first_story.get("id") or media_id
        media_url = first_story.get("url") or media_url
        ext = first_story.get("ext") or ext

        title = (
            first_story.get("title")
            or title
        )

        duration = first_story.get("duration")

        audio_url = first_story.get("audio_url")

        thumbnail = first_story.get("thumbnail")

    response = {
        "status": "ok",
        "engine": "yt-dlp",
        "instagram_story": True,
        "id": media_id,
        "ext": ext or "mp4",
        "filename": f"instagram_story_{media_id}.{ext or 'mp4'}",
        "url": media_url,
        "title": str(
            info.get("title")
            or "Instagram Story"
        ).strip(),
        "author": str(
            info.get("uploader")
            or info.get("channel")
            or info.get("creator")
            or ""
        ).strip(),
        "duration": duration,
        "upload_date": str(
            info.get("upload_date") or ""
        ).strip(),
    }

    if audio_url:
        response["audio_url"] = audio_url

    if story_items:
        response["items"] = story_items
        response["item_count"] = len(story_items)

    return jsonify(response)


@app.post("/instagram/story/download")
def instagram_story_download():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_instagram_story_url(url):
        return jsonify({
            "status": "error",
            "error": "invalid_instagram_story_url",
        }), 400

    info, error, detail, status_code = (
        extract_instagram_story_info(url)
    )

    if error:
        return jsonify({
            "status": "error",
            "error": error,
        }), status_code

    media_url, _ = choose_media(info)
    audio_url = choose_audio(info)

    if (
        not media_url
        or not is_instagram_media_url(media_url)
    ):
        return jsonify({
            "status": "error",
            "error": "instagram_story_media_missing",
        }), 422

    if (
        audio_url
        and not is_instagram_media_url(audio_url)
    ):
        audio_url = None

    temp_dir = tempfile.mkdtemp(
        prefix="monceda-instagram-story-"
    )

    final_path = os.path.join(
        temp_dir,
        "instagram-story.mp4",
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
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
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-threads",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        final_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        return jsonify({
            "status": "error",
            "error": "instagram_story_transcode_timeout",
        }), 504

    if (
        result.returncode != 0
        or not os.path.isfile(final_path)
        or os.path.getsize(final_path) == 0
    ):
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        return jsonify({
            "status": "error",
            "error": "instagram_story_transcode_failed",
        }), 422

    response = send_file(
        final_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name="instagram-story.mp4",
        conditional=False,
    )

    response.headers["Cache-Control"] = (
        "private, no-store"
    )
    response.headers[
        "X-Monceda-Instagram-Story"
    ] = "h264-aac"

    response.call_on_close(
        lambda: shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )
    )

    return response


@app.post("/instagram/normalize")
def instagram_normalize():
    data = request.get_json(silent=True) or {}
    media_url = str(data.get("url", "")).strip()
    audio_url = str(data.get("audio_url", "")).strip()
    fast_remux = data.get("fast_remux") is True

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

    if fast_remux:
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-threads",
            "2",
            "-c:a",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
    else:
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

    if fast_remux:
        response.headers[
            "X-Monceda-Instagram"
        ] = "h264-aac-fast-compatible"

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

    try:
        request_path = urlparse(url).path or ""
    except Exception:
        request_path = ""

    is_instagram_post = (
        host == "instagram.com"
        and re.match(
            r"^/p/[^/]+/?$",
            request_path,
            re.I,
        )
        is not None
    )

    cmd = [
        "yt-dlp",
    ]

    # Public extraction must never inherit Monceda Grab's
    # authenticated Instagram session. If media cannot be
    # resolved anonymously, /extract must fail closed instead
    # of expanding the caller's access through server cookies.

    if is_instagram_post:
        #
        # Instagram photo/carousel posts can contain entries
        # without conventional video formats. Keep those
        # entries so their original image metadata can be
        # normalized below.
        #
        cmd.extend([
            "--ignore-no-formats-error",
            "--no-download",
            "--no-warnings",
            "--dump-single-json",
            url,
        ])
    else:
        #
        # Preserve the existing single-media behavior for
        # Reels and every other supported platform.
        #
        cmd.extend([
            "--no-playlist",
            "--no-download",
            "--no-warnings",
            "--dump-single-json",
            url,
        ])

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
        }), 422

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return jsonify({
            "status": "error",
            "error": "invalid_extractor_response",
        }), 502

    media_url, ext = choose_media(info)

    media_id = str(info.get("id") or "media")

    #
    # Instagram photo / carousel support.
    #
    # yt-dlp can expose image posts and sidecar/carousel entries
    # without a conventional video stream. Normalize those entries
    # into the same items[] contract already used by Stories.
    #
    instagram_items = []

    if host == "instagram.com":
        raw_entries = info.get("entries")

        if not isinstance(raw_entries, list):
            raw_entries = []

        candidates = raw_entries if raw_entries else [info]

        for index, entry in enumerate(candidates, 1):
            if not isinstance(entry, dict):
                continue

            entry_url, entry_ext = choose_media(entry)

            thumbnail = entry.get("thumbnail")

            #
            # Photo posts expose several thumbnail/image
            # candidates. Prefer the last valid candidate,
            # which yt-dlp orders as the strongest available
            # image for these Instagram entries.
            #
            if not entry_url:
                thumbnails = entry.get("thumbnails")

                if isinstance(thumbnails, list):
                    image_candidates = [
                        item
                        for item in thumbnails
                        if isinstance(item, dict)
                        and is_instagram_media_url(
                            item.get("url")
                        )
                    ]

                    if image_candidates:
                        selected_image = image_candidates[-1]
                        entry_url = selected_image.get("url")
                        entry_ext = (
                            str(
                                selected_image.get("ext")
                                or entry.get("ext")
                                or "jpg"
                            )
                            .lower()
                        )

            #
            # Fallback for single-photo metadata where only
            # the canonical thumbnail field is available.
            #
            if not entry_url and isinstance(thumbnail, str):
                if is_instagram_media_url(thumbnail):
                    entry_url = thumbnail
                    entry_ext = (
                        str(entry.get("ext") or "jpg")
                        .lower()
                    )

            if not entry_url:
                continue

            clean_ext = str(entry_ext or "mp4").lower()

            media_type = (
                "image"
                if clean_ext
                in {
                    "jpg",
                    "jpeg",
                    "png",
                    "webp",
                    "gif",
                    "avif",
                }
                else "video"
            )

            item_id = str(
                entry.get("id")
                or f"{media_id}_{index}"
            )

            item = {
                "id": item_id,
                "index": index,
                "type": media_type,
                "ext": clean_ext,
                "url": entry_url,
                "filename": (
                    f"instagram_post_{item_id}."
                    f"{clean_ext}"
                ),
                "title": str(
                    entry.get("title")
                    or info.get("title")
                    or ""
                ).strip(),
                "duration": entry.get("duration"),
            }

            entry_thumbnail = entry.get("thumbnail")

            if (
                isinstance(entry_thumbnail, str)
                and is_instagram_media_url(
                    entry_thumbnail
                )
            ):
                item["thumbnail"] = entry_thumbnail

            instagram_items.append(item)

        #
        # If no conventional video was selected but Instagram
        # returned an image item, use the first item as the
        # top-level media as well.
        #
        if not media_url and instagram_items:
            media_url = instagram_items[0]["url"]
            ext = instagram_items[0]["ext"]

    if not media_url:
        return jsonify({
            "status": "error",
            "error": "no_media",
        }), 422

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

    if instagram_items:
        response["items"] = instagram_items
        response["item_count"] = len(instagram_items)

    return jsonify(response)


def facebook_story_browser_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def extract_facebook_story_photo_items(html):
    if not isinstance(html, str) or len(html) < 10000:
        return []

    # Facebook's initial Story payload contains many fbcdn images,
    # including avatars/thumbnails. Story photos use the post-image
    # t39.30808-6 path, while profile pictures normally use
    # t39.30808-1. Keep only the former.
    pattern = re.compile(
        r'https:\\?/\\?/scontent[^"\\]+?'
        r'/v/t39\.30808-6/[^"\\]+?\.jpg[^"\\]*',
        re.I,
    )

    candidates = []

    for match in pattern.findall(html):
        value = match

        # Decode the common JSON/HTML escaping used by Facebook.
        value = value.replace(r"\/", "/")
        value = value.replace(r"\u0025", "%")
        value = value.replace(r"\u0026", "&")
        value = value.replace("&amp;", "&")

        try:
            value = bytes(
                value,
                "utf-8",
            ).decode("unicode_escape")
        except Exception:
            pass

        if not value.startswith("https://"):
            continue

        filename_match = re.search(
            r"/([^/?]+\.jpg)",
            value,
            re.I,
        )

        if not filename_match:
            continue

        filename = filename_match.group(1)

        # Strong signal from the authenticated Story payload:
        # actual Story image filenames contain the owner's numeric
        # post/story identifier and are not profile-picture URLs.
        score = 0

        if "/v/t39.30808-6/" in value:
            score += 100

        if "dst-jpg" in value:
            score += 25

        size_matches = re.findall(
            r"(?:mx|s)(\d{3,4})x(\d{3,4})",
            value,
            re.I,
        )

        max_area = 0

        for width, height in size_matches:
            try:
                area = int(width) * int(height)
            except Exception:
                continue

            max_area = max(max_area, area)

        if max_area >= 1000000:
            score += 60
        elif max_area >= 500000:
            score += 40

        candidates.append({
            "url": value,
            "filename": filename,
            "score": score,
            "area": max_area,
        })

    # Deduplicate by the stable Facebook image filename.
    best_by_filename = {}

    for item in candidates:
        key = item["filename"]

        previous = best_by_filename.get(key)

        if previous is None or (
            item["score"],
            item["area"],
            len(item["url"]),
        ) > (
            previous["score"],
            previous["area"],
            len(previous["url"]),
        ):
            best_by_filename[key] = item

    ranked = sorted(
        best_by_filename.values(),
        key=lambda item: (
            item["score"],
            item["area"],
        ),
        reverse=True,
    )

    # Do not expose weak avatar/UI candidates.
    ranked = [
        item
        for item in ranked
        if item["score"] >= 125
    ]

    result = []

    for index, item in enumerate(ranked, 1):
        result.append({
            "index": index,
            "id": item["filename"].rsplit(".", 1)[0],
            "type": "image",
            "ext": "jpg",
            "filename": item["filename"],
            "url": item["url"],
            "thumbnail": item["url"],
        })

    return result


def fetch_facebook_story_html(url):
    """
    Fetch only Facebook Story pages available without Monceda Grab's
    authenticated Facebook session.

    Authentication redirects, inaccessible pages, and non-Facebook
    redirect targets fail closed.
    """
    try:
        import requests
    except Exception:
        return (
            None,
            "facebook_story_processor_unavailable",
            "",
            500,
        )

    session = requests.Session()
    session.headers.update(
        facebook_story_browser_headers()
    )

    try:
        response = session.get(
            url,
            timeout=45,
            allow_redirects=True,
        )

        try:
            final_url = urlparse(response.url)
            final_host = re.sub(
                r"^www\.",
                "",
                (final_url.hostname or "").lower(),
            )
        except Exception:
            return (
                None,
                "facebook_story_public_unavailable",
                "",
                422,
            )

        if (
            final_url.scheme != "https"
            or final_host not in {
                "facebook.com",
                "m.facebook.com",
            }
        ):
            return (
                None,
                "facebook_story_public_unavailable",
                "",
                422,
            )

        if response.status_code != 200:
            return (
                None,
                "facebook_story_public_unavailable",
                "",
                422,
            )

        html = response.text or ""

        if len(html) < 10000:
            return (
                None,
                "facebook_story_public_unavailable",
                "",
                422,
            )

        return html, None, "", 200

    except requests.RequestException:
        return (
            None,
            "facebook_story_public_unavailable",
            "",
            422,
        )


def is_facebook_story_url(value):
    try:
        parsed = urlparse(value)

        host = re.sub(
            r"^www\.",
            "",
            (parsed.hostname or "").lower(),
        )

        return (
            parsed.scheme == "https"
            and host in {
                "facebook.com",
                "m.facebook.com",
            }
            and parsed.path.startswith("/stories/")
        )
    except Exception:
        return False


@app.post("/facebook/story/extract")
def facebook_story_extract():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_facebook_story_url(url):
        return jsonify({
            "status": "error",
            "error": "invalid_facebook_story_url",
        }), 400

    html, error, detail, status_code = (
        fetch_facebook_story_html(url)
    )

    if error:
        return jsonify({
            "status": "error",
            "error": error,
        }), status_code

    items = extract_facebook_story_photo_items(html)

    if not items:
        return jsonify({
            "status": "error",
            "error": "facebook_story_media_not_found",
        }), 422

    return jsonify({
        "status": "ok",
        "engine": "facebook-story-html",
        "item_count": len(items),
        "items": items,
    })


@app.post("/facebook/story/debug")

def facebook_story_debug():
    """
    Production diagnostic endpoint intentionally disabled.
    """
    return jsonify({
        "status": "error",
        "error": "not_found",
    }), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

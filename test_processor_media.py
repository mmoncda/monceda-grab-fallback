import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_processor_contract import TEST_TOKEN, load_processor


AUTH_HEADERS = {
    "X-Monceda-Processor-Token": TEST_TOKEN,
}


def completed(payload, returncode=0):
    if isinstance(payload, str):
        stdout = payload
    else:
        stdout = json.dumps(payload)

    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


class ProcessorMediaRegressionBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.processor = load_processor()
        cls.client = cls.processor.app.test_client()

    def test_instagram_story_url_validation_is_exact(self):
        self.assertTrue(
            self.processor.is_instagram_story_url(
                "https://www.instagram.com/stories/alice/"
            )
        )

        self.assertTrue(
            self.processor.is_instagram_story_url(
                "https://instagram.com/stories/alice/123456/"
            )
        )

        invalid = [
            "http://instagram.com/stories/alice/",
            "https://instagram.com.evil.example/stories/alice/",
            "https://instagram.com/p/ABC123/",
            "not-a-url",
        ]

        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(
                    self.processor.is_instagram_story_url(value)
                )

    def test_instagram_media_url_allowlist_rejects_lookalikes(self):
        valid = [
            "https://cdninstagram.com/media.jpg",
            "https://scontent.cdninstagram.com/media.jpg",
            "https://fbcdn.net/media.jpg",
            "https://scontent.xx.fbcdn.net/media.jpg",
        ]

        invalid = [
            "http://cdninstagram.com/media.jpg",
            "https://cdninstagram.com.evil.example/media.jpg",
            "https://example.com/media.jpg",
            "not-a-url",
        ]

        for value in valid:
            with self.subTest(valid=value):
                self.assertTrue(
                    self.processor.is_instagram_media_url(value)
                )

        for value in invalid:
            with self.subTest(invalid=value):
                self.assertFalse(
                    self.processor.is_instagram_media_url(value)
                )

    def test_choose_media_selects_largest_image_candidate(self):
        info = {
            "formats": [
                {
                    "url": "https://cdn.example/small.jpg",
                    "ext": "jpg",
                    "width": 640,
                    "height": 640,
                },
                {
                    "url": "https://cdn.example/large.webp",
                    "ext": "webp",
                    "width": 1080,
                    "height": 1350,
                },
            ],
        }

        media_url, ext = self.processor.choose_media(info)

        self.assertEqual(
            media_url,
            "https://cdn.example/large.webp",
        )
        self.assertEqual(ext, "webp")

    def test_choose_audio_prefers_highest_requested_bitrate(self):
        info = {
            "requested_formats": [
                {
                    "url": "https://cdn.example/audio-low.m4a",
                    "acodec": "aac",
                    "vcodec": "none",
                    "abr": 64,
                },
                {
                    "url": "https://cdn.example/audio-high.m4a",
                    "acodec": "aac",
                    "vcodec": "none",
                    "abr": 128,
                },
            ],
        }

        self.assertEqual(
            self.processor.choose_audio(info),
            "https://cdn.example/audio-high.m4a",
        )

    def test_extract_rejects_unsupported_host_without_subprocess(self):
        with patch.object(
            self.processor.subprocess,
            "run",
        ) as run_mock:
            response = self.client.post(
                "/extract",
                json={
                    "url": "https://example.com/video/123",
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            400,
            response.get_data(as_text=True),
        )

        self.assertEqual(
            response.get_json().get("error"),
            "unsupported_host",
        )

        run_mock.assert_not_called()

    def test_extract_timeout_is_normalized(self):
        with patch.object(
            self.processor.subprocess,
            "run",
            side_effect=self.processor.subprocess.TimeoutExpired(
                cmd=["yt-dlp"],
                timeout=45,
            ),
        ):
            response = self.client.post(
                "/extract",
                json={
                    "url": "https://bsky.app/profile/example/post/123",
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            504,
            response.get_data(as_text=True),
        )

        self.assertEqual(
            response.get_json().get("error"),
            "extract_timeout",
        )

    def test_extract_instagram_carousel_normalizes_items(self):
        payload = {
            "id": "CAROUSEL1",
            "title": "Carousel post",
            "uploader": "alice",
            "entries": [
                {
                    "id": "PHOTO1",
                    "title": "Photo one",
                    "thumbnails": [
                        {
                            "url": (
                                "https://scontent.cdninstagram.com/"
                                "photo-small.jpg"
                            ),
                            "ext": "jpg",
                        },
                        {
                            "url": (
                                "https://scontent.cdninstagram.com/"
                                "photo-large.jpg"
                            ),
                            "ext": "jpg",
                        },
                    ],
                },
                {
                    "id": "VIDEO1",
                    "title": "Video one",
                    "url": (
                        "https://scontent.cdninstagram.com/"
                        "video-one.mp4"
                    ),
                    "ext": "mp4",
                },
            ],
        }

        with patch.object(
            self.processor.subprocess,
            "run",
            return_value=completed(payload),
        ) as run_mock:
            response = self.client.post(
                "/extract",
                json={
                    "url": (
                        "https://www.instagram.com/"
                        "p/CAROUSEL1/"
                    ),
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

        body = response.get_json()

        self.assertEqual(body.get("status"), "ok")
        self.assertEqual(body.get("item_count"), 2)

        items = body.get("items") or []

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].get("type"), "image")
        self.assertEqual(items[0].get("ext"), "jpg")
        self.assertEqual(
            items[0].get("url"),
            (
                "https://scontent.cdninstagram.com/"
                "photo-large.jpg"
            ),
        )
        self.assertEqual(items[1].get("type"), "video")

        self.assertEqual(
            body.get("url"),
            items[0].get("url"),
        )

        cmd = run_mock.call_args.args[0]

        self.assertIn(
            "--ignore-no-formats-error",
            cmd,
        )
        self.assertNotIn(
            "--no-playlist",
            cmd,
        )

    def test_extract_bluesky_success_contract(self):
        payload = {
            "id": "BSKY1",
            "title": "Bluesky video",
            "uploader": "alice.test",
            "url": "https://media.example/video.mp4",
            "ext": "mp4",
            "duration": 12,
        }

        with patch.object(
            self.processor.subprocess,
            "run",
            return_value=completed(payload),
        ):
            response = self.client.post(
                "/extract",
                json={
                    "url": (
                        "https://bsky.app/profile/"
                        "alice.test/post/abc123"
                    ),
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

        body = response.get_json()

        self.assertEqual(body.get("status"), "ok")
        self.assertEqual(body.get("engine"), "yt-dlp")
        self.assertEqual(body.get("id"), "BSKY1")
        self.assertEqual(body.get("ext"), "mp4")
        self.assertEqual(
            body.get("filename"),
            "bsky_app_BSKY1.mp4",
        )
        self.assertEqual(
            body.get("url"),
            "https://media.example/video.mp4",
        )

    def test_instagram_story_username_url_uses_first_story_item(self):
        first = {
            "id": "FIRST",
            "url": (
                "https://scontent.cdninstagram.com/"
                "first.mp4"
            ),
            "ext": "mp4",
            "title": "First Story",
            "duration": 8,
        }

        second = {
            "id": "SECOND",
            "url": (
                "https://scontent.cdninstagram.com/"
                "second.mp4"
            ),
            "ext": "mp4",
            "title": "Second Story",
            "duration": 9,
        }

        info = {
            **first,
            "uploader": "alice",
            "_monceda_root_entries": [
                first,
                second,
            ],
        }

        with patch.object(
            self.processor,
            "extract_instagram_story_info",
            return_value=(info, None, "", 200),
        ), patch.object(
            self.processor,
            "fetch_instagram_raw_story_items",
            return_value=[],
        ):
            response = self.client.post(
                "/instagram/story/extract",
                json={
                    "url": (
                        "https://www.instagram.com/"
                        "stories/alice/"
                    ),
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

        body = response.get_json()

        self.assertEqual(body.get("id"), "FIRST")
        self.assertEqual(body.get("duration"), 8)
        self.assertEqual(body.get("item_count"), 2)
        self.assertEqual(
            body.get("url"),
            first["url"],
        )

    def test_facebook_story_route_success_contract(self):
        items = [
            {
                "index": 1,
                "id": "story-photo-1",
                "type": "image",
                "ext": "jpg",
                "filename": "story-photo-1.jpg",
                "url": (
                    "https://scontent.xx.fbcdn.net/"
                    "story-photo-1.jpg"
                ),
                "thumbnail": (
                    "https://scontent.xx.fbcdn.net/"
                    "story-photo-1.jpg"
                ),
            },
        ]

        with patch.object(
            self.processor,
            "fetch_facebook_story_html",
            return_value=(
                "<html>" + ("x" * 12000) + "</html>",
                None,
                "",
                200,
            ),
        ), patch.object(
            self.processor,
            "extract_facebook_story_photo_items",
            return_value=items,
        ):
            response = self.client.post(
                "/facebook/story/extract",
                json={
                    "url": (
                        "https://www.facebook.com/"
                        "stories/alice/123/"
                    ),
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

        body = response.get_json()

        self.assertEqual(body.get("status"), "ok")
        self.assertEqual(
            body.get("engine"),
            "facebook-story-html",
        )
        self.assertEqual(body.get("item_count"), 1)
        self.assertEqual(body.get("items"), items)


class ProcessorInstagramNumericStoryRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.processor = load_processor()
        cls.client = cls.processor.app.test_client()

    def test_numeric_instagram_story_success_contract(self):
        selected = {
            "id": "NUMERIC1",
            "url": (
                "https://scontent.cdninstagram.com/"
                "numeric-story.mp4"
            ),
            "ext": "mp4",
            "title": "Numeric Story",
            "duration": 11,
        }

        info = {
            **selected,
            "uploader": "alice",
            "_monceda_root_entries": [
                selected,
            ],
        }

        with patch.object(
            self.processor,
            "extract_instagram_story_info",
            return_value=(info, None, "", 200),
        ), patch.object(
            self.processor,
            "fetch_instagram_raw_story_items",
            return_value=[],
        ):
            response = self.client.post(
                "/instagram/story/extract",
                json={
                    "url": (
                        "https://www.instagram.com/"
                        "stories/alice/123456789/"
                    ),
                },
                headers=AUTH_HEADERS,
            )

        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

        body = response.get_json()

        self.assertEqual(body.get("status"), "ok")
        self.assertTrue(body.get("instagram_story"))
        self.assertEqual(body.get("id"), "NUMERIC1")
        self.assertEqual(body.get("duration"), 11)
        self.assertEqual(
            body.get("url"),
            selected["url"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

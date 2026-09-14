import importlib
import os
import sys
import unittest


TEST_TOKEN = "monceda-test-processor-token"


def load_processor():
    os.environ["MONCEDA_PROCESSOR_TOKEN"] = TEST_TOKEN

    if "app" in sys.modules:
        del sys.modules["app"]

    module = importlib.import_module("app")

    module.app.config.update(
        TESTING=True,
    )

    return module


class ProcessorSecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.processor = load_processor()
        cls.client = cls.processor.app.test_client()

    def test_health_is_public(self):
        response = self.client.get("/")

        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

    def test_debug_impersonation_is_not_exposed(self):
        response = self.client.get("/debug/impersonation")

        self.assertEqual(
            response.status_code,
            404,
            response.get_data(as_text=True),
        )

    def test_extract_rejects_missing_processor_token(self):
        response = self.client.post(
            "/extract",
            json={"url": ""},
        )

        self.assertEqual(
            response.status_code,
            401,
            response.get_data(as_text=True),
        )

        payload = response.get_json(silent=True) or {}

        self.assertEqual(
            payload.get("error"),
            "unauthorized",
        )

    def test_extract_rejects_wrong_processor_token(self):
        response = self.client.post(
            "/extract",
            json={"url": ""},
            headers={
                "X-Monceda-Processor-Token": "definitely-wrong-token",
            },
        )

        self.assertEqual(
            response.status_code,
            401,
            response.get_data(as_text=True),
        )

    def test_extract_accepts_correct_token_then_runs_validation(self):
        response = self.client.post(
            "/extract",
            json={"url": ""},
            headers={
                "X-Monceda-Processor-Token": TEST_TOKEN,
            },
        )

        self.assertEqual(
            response.status_code,
            400,
            response.get_data(as_text=True),
        )

        payload = response.get_json(silent=True) or {}

        self.assertEqual(
            payload.get("error"),
            "invalid_url",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

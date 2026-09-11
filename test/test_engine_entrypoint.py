import os
import unittest
from unittest.mock import patch

import engine
from app.config import config


class TestEngineEntrypoint(unittest.TestCase):
    def setUp(self):
        self.original_api_key = config.app.get("api_key", "")
        self.original_host = config.listen_host
        self.original_port = config.listen_port

    def tearDown(self):
        config.app["api_key"] = self.original_api_key
        config.listen_host = self.original_host
        config.listen_port = self.original_port

    def test_requires_api_key_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MPT_API_KEY is required"):
                engine.configure_runtime()

    def test_allows_explicit_local_insecure_mode(self):
        with patch.dict(
            os.environ,
            {
                "MPT_ALLOW_INSECURE": "1",
                "MPT_LISTEN_HOST": "127.0.0.1",
                "MPT_LISTEN_PORT": "18080",
            },
            clear=True,
        ):
            engine.configure_runtime()

        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.listen_port, 18080)

    def test_api_key_and_listener_come_from_environment(self):
        with patch.dict(
            os.environ,
            {
                "MPT_API_KEY": "test-engine-secret",
                "MPT_LISTEN_HOST": "0.0.0.0",
                "MPT_LISTEN_PORT": "8088",
            },
            clear=True,
        ):
            engine.configure_runtime()

        self.assertEqual(config.app["api_key"], "test-engine-secret")
        self.assertEqual(config.listen_host, "0.0.0.0")
        self.assertEqual(config.listen_port, 8088)

    def test_rejects_invalid_port(self):
        with patch.dict(
            os.environ,
            {"MPT_API_KEY": "test-engine-secret", "MPT_LISTEN_PORT": "nope"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                engine.configure_runtime()


if __name__ == "__main__":
    unittest.main()

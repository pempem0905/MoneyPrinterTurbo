"""Provider-config secret contract, provider catalog, x-api-key auth and the
safe stock-key verification route used by Common OS "API nguồn video"."""

import json
import unittest
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.services import stock_verification

API_KEY = "engine-shared-secret"
HEADERS = {"x-api-key": API_KEY}
PEXELS_KEY = "PEXELS-SECRET-KEY-1234"
PIXABAY_KEY = "PIXABAY-SECRET-KEY-5678"


def _response(status_code, body=None, headers=None, text=""):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {"content-type": "application/json"}
    response.text = text or (json.dumps(body) if body is not None else "")
    if body is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    return response


class _EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        for key in ("pexels_api_keys", "pixabay_api_keys", "coverr_api_keys"):
            config.app.pop(key, None)
        config.app["api_key"] = API_KEY
        self.save = patch.object(config, "try_save_config")
        self.save.start()
        self.client = TestClient(asgi.app)

    def tearDown(self):
        self.save.stop()
        config.app.clear()
        config.app.update(self.original_app_config)


class TestProviderConfigSecrets(_EngineTestCase):
    def test_get_never_returns_secret_values(self):
        config.app["pexels_api_keys"] = [PEXELS_KEY]
        response = self.client.get("/api/v1/provider-config", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIsNone(data["values"]["app"]["pexels_api_keys"])
        self.assertTrue(data["configured"]["app.pexels_api_keys"])
        self.assertFalse(data["configured"]["app.pixabay_api_keys"])
        self.assertEqual(data["masked"]["app.pexels_api_keys"], "Đã lưu 1 key")
        self.assertNotIn(PEXELS_KEY, response.text)

    def test_patch_null_keeps_and_empty_list_clears(self):
        config.app["pexels_api_keys"] = [PEXELS_KEY]
        keep = self.client.patch(
            "/api/v1/provider-config",
            headers=HEADERS,
            json={"updates": {"app": {"pexels_api_keys": None}}},
        )
        self.assertEqual(keep.status_code, 200)
        self.assertEqual(keep.json()["data"]["changed"], [])
        self.assertEqual(config.app["pexels_api_keys"], [PEXELS_KEY])

        clear = self.client.patch(
            "/api/v1/provider-config",
            headers=HEADERS,
            json={"updates": {"app": {"pexels_api_keys": []}}},
        )
        self.assertEqual(clear.status_code, 200)
        self.assertEqual(clear.json()["data"]["changed"], ["app.pexels_api_keys"])
        self.assertEqual(config.app["pexels_api_keys"], [])

    def test_patch_set_is_reflected_by_capabilities_without_leaking(self):
        response = self.client.patch(
            "/api/v1/provider-config",
            headers=HEADERS,
            json={"updates": {"app": {"pixabay_api_keys": [PIXABAY_KEY]}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PIXABAY_KEY, response.text)

        caps = self.client.get("/api/v1/capabilities", headers=HEADERS)
        self.assertEqual(caps.status_code, 200)
        sources = {s["id"]: s for s in caps.json()["data"]["sources"]}
        self.assertIs(sources["pixabay"]["configured"], True)
        self.assertIs(sources["pexels"]["configured"], False)
        self.assertIs(sources["coverr"]["configured"], False)
        self.assertNotIn(PIXABAY_KEY, caps.text)

    def test_catalog_lists_stock_sources(self):
        response = self.client.get("/api/v1/provider-catalog", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        stock = response.json()["data"]["stock"]
        for source in ("pexels", "pixabay", "coverr"):
            self.assertIn(source, stock)

    def test_provider_routes_require_api_key(self):
        for method, path, body in (
            ("get", "/api/v1/provider-config", None),
            ("patch", "/api/v1/provider-config", {"updates": {}}),
            ("get", "/api/v1/provider-catalog", None),
            ("get", "/api/v1/capabilities", None),
            ("post", "/api/v1/materials/verify", {"source": "pexels"}),
            ("post", "/api/v1/materials/search", {"source": "pexels", "search_term": "x"}),
        ):
            kwargs = {"json": body} if body is not None else {}
            missing = getattr(self.client, method)(path, **kwargs)
            wrong = getattr(self.client, method)(path, headers={"x-api-key": "nope"}, **kwargs)
            self.assertEqual(missing.status_code, 401, path)
            self.assertEqual(wrong.status_code, 401, path)


class TestStockVerification(_EngineTestCase):
    def _verify(self, source):
        return self.client.post("/api/v1/materials/verify", headers=HEADERS, json={"source": source})

    def test_not_configured_makes_no_request(self):
        with patch.object(stock_verification.requests, "get") as get:
            response = self._verify("pexels")
        get.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "not_configured")

    def test_ok_uses_one_minimal_search_and_never_downloads(self):
        config.app["pexels_api_keys"] = [PEXELS_KEY]
        with patch.object(
            stock_verification.requests, "get", return_value=_response(200, {"videos": []})
        ) as get:
            response = self._verify("pexels")
        self.assertEqual(get.call_count, 1)
        url = get.call_args.args[0]
        self.assertIn("api.pexels.com/videos/search", url)
        self.assertIn("per_page=1", url)
        data = response.json()["data"]
        self.assertEqual(data["status"], "ok")
        self.assertEqual((data["keys_checked"], data["keys_ok"]), (1, 1))
        self.assertNotIn(PEXELS_KEY, response.text)

    def test_rejected_key_is_invalid_key(self):
        config.app["pixabay_api_keys"] = PIXABAY_KEY
        with patch.object(
            stock_verification.requests,
            "get",
            return_value=_response(400, None, text="[ERROR 400] Invalid or missing API key"),
        ):
            response = self._verify("pixabay")
        data = response.json()["data"]
        self.assertEqual(data["status"], "invalid_key")
        self.assertEqual(data["http_status"], 400)
        self.assertNotIn(PIXABAY_KEY, response.text)

    def test_one_bad_key_among_many_is_invalid(self):
        config.app["coverr_api_keys"] = ["good-coverr", "bad-coverr"]
        with patch.object(
            stock_verification.requests,
            "get",
            side_effect=[_response(200, {"hits": []}), _response(401, {"message": "no"})],
        ):
            data = self._verify("coverr").json()["data"]
        self.assertEqual(data["status"], "invalid_key")
        self.assertEqual((data["keys_checked"], data["keys_ok"]), (2, 1))

    def test_network_error_is_unreachable_and_not_leaked(self):
        config.app["pixabay_api_keys"] = [PIXABAY_KEY]
        error = requests.ConnectionError(f"failed https://pixabay.com/api/videos/?key={PIXABAY_KEY}")
        with patch.object(stock_verification.requests, "get", side_effect=error):
            with patch.object(stock_verification.logger, "warning") as warning:
                response = self._verify("pixabay")
        self.assertEqual(response.json()["data"]["status"], "unreachable")
        self.assertNotIn(PIXABAY_KEY, response.text)
        for call in warning.call_args_list:
            self.assertNotIn(PIXABAY_KEY, str(call))

    def test_server_error_and_rate_limit(self):
        config.app["pexels_api_keys"] = [PEXELS_KEY]
        with patch.object(stock_verification.requests, "get", return_value=_response(503, None)):
            self.assertEqual(self._verify("pexels").json()["data"]["status"], "unreachable")
        with patch.object(stock_verification.requests, "get", return_value=_response(429, {})):
            self.assertEqual(self._verify("pexels").json()["data"]["status"], "rate_limited")

    def test_unexpected_json_is_unreachable(self):
        config.app["pexels_api_keys"] = [PEXELS_KEY]
        with patch.object(
            stock_verification.requests, "get", return_value=_response(200, {"error": "?"})
        ):
            self.assertEqual(self._verify("pexels").json()["data"]["status"], "unreachable")

    def test_rejects_non_stock_sources(self):
        for source in ("local", "wavespeed", "../etc"):
            self.assertEqual(self._verify(source).status_code, 400, source)


if __name__ == "__main__":
    unittest.main()

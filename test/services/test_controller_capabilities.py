import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.v1 import capabilities as capabilities_controller
from app.models.schema import MaterialInfo, VideoParams
from app.services import task as task_service

SECRET_VALUES = {
    "pexels_api_keys": ["PEXELS-SECRET-111"],
    "pixabay_api_keys": ["PIXABAY-SECRET-222"],
    "coverr_api_keys": "COVERR-SECRET-333",
    "wavespeed_api_keys": ["WAVESPEED-SECRET-444"],
    "volcengine_seedance_api_key": "SEEDANCE-SECRET-555",
    "ofox_api_key": "OFOX-SECRET-666",
    "metaso_minimax_api_key": "METASO-SECRET-777",
    "openai_image_base_url": "https://image-gateway.internal.example/v1",
    "openai_image_model": "IMAGE-MODEL-SECRET-888",
    "openai_image_api_keys": ["OPENAI-IMAGE-SECRET-999"],
    "loomloom_api_token": "LOOMLOOM-SECRET-000",
    "sonilo_api_key": "SONILO-SECRET-aaa",
    "twelvelabs_api_keys": ["TWELVELABS-SECRET-bbb"],
}

CLEARED_KEYS = list(SECRET_VALUES) + ["volcengine_api_key", "llm_provider"]
PROVIDER_ENV_VARS = (
    "VOLCENGINE_ARK_API_KEY",
    "OFOX_API_KEY",
    "METASO_MINIMAX_API_KEY",
    "SONILO_API_KEY",
    "ELEVENLABS_API_KEY",
)


class TestCapabilitiesEndpoint(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_elevenlabs = dict(config.elevenlabs)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for name in PROVIDER_ENV_VARS:
            os.environ.pop(name, None)
        for key in CLEARED_KEYS:
            config.app.pop(key, None)
        config.elevenlabs.pop("api_key", None)
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)

    def tearDown(self):
        self.env.stop()
        config.app.clear()
        config.app.update(self.original_app_config)
        config.elevenlabs.clear()
        config.elevenlabs.update(self.original_elevenlabs)

    def _get(self, **kwargs):
        response = self.client.get("/api/v1/capabilities", **kwargs)
        return response

    def _sources(self, data):
        return {source["id"]: source for source in data["sources"]}

    def test_lists_every_supported_source_with_capability_fields(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)
        data = body["data"]
        self.assertEqual(data["contract_version"], 1)
        sources = self._sources(data)
        self.assertEqual(
            set(sources),
            {
                "local",
                "pexels",
                "pixabay",
                "coverr",
                "wavespeed",
                "volcengine_seedance",
                "ofox",
                "metaso_minimax",
                "loomloom",
                "openai_image",
            },
        )
        for source in sources.values():
            for field in (
                "label",
                "type",
                "paid",
                "configured",
                "api_renderable",
                "supports_portrait",
                "supports_landscape",
                "supports_scene_prompt",
                "supports_direct_material",
                "cost_warning",
            ):
                self.assertIn(field, source)
        self.assertEqual(sources["local"]["type"], "local")
        self.assertEqual(sources["pexels"]["type"], "stock")
        self.assertEqual(sources["wavespeed"]["type"], "ai_video")
        self.assertEqual(sources["openai_image"]["type"], "ai_image")
        # Every AI source is flagged paid with a cost warning; stock/local are free.
        for source in sources.values():
            if source["type"] in {"ai_video", "ai_image"}:
                self.assertTrue(source["paid"])
                self.assertTrue(source["cost_warning"])
            else:
                self.assertFalse(source["paid"])
        self.assertFalse(sources["loomloom"]["api_renderable"])
        self.assertTrue(sources["local"]["supports_direct_material"])

    def test_unconfigured_sources_report_false(self):
        sources = self._sources(self._get().json()["data"])
        for source_id in (
            "pexels",
            "pixabay",
            "coverr",
            "wavespeed",
            "volcengine_seedance",
            "ofox",
            "metaso_minimax",
            "loomloom",
            "openai_image",
        ):
            self.assertFalse(sources[source_id]["configured"], source_id)
        # The local source always works (engine ships a demo clip).
        self.assertTrue(sources["local"]["configured"])

    def test_configured_sources_report_true_and_never_leak_secret_values(self):
        config.app.update(SECRET_VALUES)
        config.elevenlabs["api_key"] = "ELEVENLABS-SECRET-ccc"

        response = self._get()
        raw = response.text
        data = response.json()["data"]

        sources = self._sources(data)
        for source_id in sources:
            self.assertTrue(sources[source_id]["configured"], source_id)
        music = {item["id"]: item for item in data["music_providers"]}
        self.assertTrue(music["sonilo"]["configured"])
        self.assertTrue(music["elevenlabs"]["configured"])
        self.assertTrue(data["script_contract"]["terms_may_be_reordered"])

        leaked = [
            *[
                item
                for value in SECRET_VALUES.values()
                for item in (value if isinstance(value, list) else [value])
            ],
            "ELEVENLABS-SECRET-ccc",
            "image-gateway.internal.example",
        ]
        for secret in leaked:
            self.assertNotIn(secret, raw)
        self.assertNotIn("config.toml", json.dumps(data["sources"]))

    def test_render_options_match_the_request_schema(self):
        options = self._get().json()["data"]["render_options"]
        self.assertEqual(options["video_aspect"], ["16:9", "9:16", "1:1"])
        self.assertEqual(options["video_fit_mode"], ["cover", "contain"])
        self.assertEqual(options["video_concat_mode"], ["random", "sequential"])
        self.assertIn("FadeIn", options["video_transition_mode"])
        self.assertNotIn(None, options["video_transition_mode"])
        self.assertEqual(options["subtitle_display_mode"], ["sentence", "word_by_word"])
        self.assertEqual(options["subtitle_animation"], ["none", "pop_spring"])
        self.assertEqual(options["video_clip_speed"], {"min": 0.5, "max": 2.0})
        self.assertEqual(options["paragraph_number"], {"min": 1, "max": 10})
        # Every advertised enum value must be accepted by VideoParams.
        for fit in options["video_fit_mode"]:
            VideoParams(video_subject="s", video_fit_mode=fit)
        for transition in options["video_transition_mode"]:
            VideoParams(video_subject="s", video_transition_mode=transition)
        for mode in options["subtitle_display_mode"]:
            VideoParams(video_subject="s", subtitle_display_mode=mode)
        for animation in options["subtitle_animation"]:
            VideoParams(video_subject="s", subtitle_animation=animation)

    def test_requires_api_key_when_configured(self):
        config.app["api_key"] = "engine-secret"
        self.assertEqual(self._get().status_code, 401)
        self.assertEqual(
            self._get(headers={"x-api-key": "engine-secret"}).status_code, 200
        )

    def test_is_read_only(self):
        self.assertEqual(self.client.post("/api/v1/capabilities").status_code, 405)
        self.assertEqual(self.client.patch("/api/v1/capabilities").status_code, 405)

    def test_broken_provider_check_reports_false_instead_of_failing(self):
        spec = next(
            item for item in capabilities_controller._SOURCES if item["id"] == "ofox"
        )

        def boom():
            raise RuntimeError("misconfigured base url")

        with patch.dict(spec, {"configured": boom}):
            sources = self._sources(capabilities_controller.build_capabilities())
        self.assertFalse(sources["ofox"]["configured"])


class TestCommonOsScriptContract(unittest.TestCase):
    """The Common OS integration relies on exact script/terms being preserved."""

    def test_non_empty_script_is_used_verbatim_without_llm(self):
        params = VideoParams(video_subject="s", video_script="  Kịch bản cố định.  ")
        with patch.object(task_service.llm, "generate_script") as generate:
            script = task_service.generate_script("task-exact", params)
        generate.assert_not_called()
        self.assertEqual(script, "Kịch bản cố định.")

    def test_non_empty_terms_are_used_verbatim_without_llm(self):
        params = VideoParams(
            video_subject="s",
            video_terms=["phone repair shop", "smartphone screen"],
            match_materials_to_script=True,
        )
        with patch.object(task_service.llm, "generate_terms") as generate, patch.object(
            task_service.twelvelabs, "rerank_terms_by_subject"
        ) as rerank:
            terms = task_service.generate_terms("task-exact", params, "script")
        generate.assert_not_called()
        rerank.assert_not_called()
        self.assertEqual(terms, ["phone repair shop", "smartphone screen"])

    def test_explicit_local_materials_keep_scene_order_and_attribution(self):
        materials = [
            MaterialInfo(
                provider="local",
                url="scene-1-shop.mp4",
                duration=4,
                source_info={"provider": "shop", "scene_id": "s1"},
            ),
            MaterialInfo(
                provider="local",
                url="scene-2-pexels.mp4",
                duration=4,
                source_info={"provider": "pexels", "scene_id": "s2"},
            ),
        ]
        params = VideoParams(
            video_subject="s",
            video_source="local",
            video_materials=materials,
            video_concat_mode="sequential",
        )
        with patch.object(
            task_service.video,
            "preprocess_video",
            side_effect=lambda materials, clip_duration: materials,
        ) as preprocess:
            paths = task_service.get_video_materials("task-local", params, "", 8)
        preprocess.assert_called_once()
        self.assertEqual(paths, ["scene-1-shop.mp4", "scene-2-pexels.mp4"])
        dumped = params.model_dump()
        self.assertEqual(
            [item["source_info"]["scene_id"] for item in dumped["video_materials"]],
            ["s1", "s2"],
        )


if __name__ == "__main__":
    unittest.main()

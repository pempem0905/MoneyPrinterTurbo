import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.models.schema import MaterialInfo, VideoParams
from app.services import material_resolution as resolution
from app.services import task as task_service
from app.services import video as vd


class _StorageMixin:
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.storage = tempfile.mkdtemp(prefix="mpt-resolution-")

        def storage_dir(sub_dir="", create=False):
            target = os.path.join(self.storage, sub_dir)
            if create:
                os.makedirs(target, exist_ok=True)
            return target

        self.storage_patch = patch.object(
            resolution.utils, "storage_dir", side_effect=storage_dir
        )
        self.storage_patch.start()
        config.app["api_key"] = ""

    def tearDown(self):
        self.storage_patch.stop()
        shutil.rmtree(self.storage, ignore_errors=True)
        config.app.clear()
        config.app.update(self.original_app_config)
        with resolution._active_lock:
            resolution._active_jobs.clear()

    def local_file(self, name):
        return os.path.join(self.storage, "local_videos", name)


def _stock_item(asset_id, url="https://videos.pexels.com/v/1.mp4?token=SECRET"):
    item = MaterialInfo()
    item.provider = "pexels"
    item.url = url
    item.duration = 12
    item.source_info = {
        "provider": "pexels",
        "search_term": "phone repair",
        "asset_id": asset_id,
        "source_page": "https://www.pexels.com/video/123/?utm=1",
        "creator": {"name": "Linh", "url": "https://www.pexels.com/@linh"},
        "rendition": {"id": 77, "width": 1080, "height": 1920},
    }
    return item


class TestStockSearchAndImport(_StorageMixin, unittest.TestCase):
    def test_search_hides_download_url_and_registers_candidate(self):
        config.app["pexels_api_keys"] = ["PEXELS-KEY-SECRET"]
        with patch.object(
            resolution.material, "search_videos_pexels", return_value=[_stock_item("123")]
        ) as search:
            candidates = resolution.search_stock(
                source="pexels", search_term="phone repair", video_aspect="9:16"
            )
        search.assert_called_once()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["asset_id"], "123")
        self.assertEqual(candidate["width"], 1080)
        self.assertEqual(candidate["source_page"], "https://www.pexels.com/video/123/")
        raw = json.dumps(candidates)
        self.assertNotIn("videos.pexels.com", raw)
        self.assertNotIn("SECRET", raw)
        registry = os.path.join(
            self.storage, "material_candidates", f"{candidate['candidate_id']}.json"
        )
        self.assertTrue(os.path.exists(registry))

    def test_search_rejects_unconfigured_or_unknown_source(self):
        config.app.pop("pexels_api_keys", None)
        with self.assertRaises(resolution.MaterialConflictError):
            resolution.search_stock(source="pexels", search_term="x")
        with self.assertRaises(resolution.MaterialResolutionError):
            resolution.search_stock(source="wavespeed", search_term="x")

    def test_import_is_idempotent_and_never_accepts_a_url(self):
        config.app["pexels_api_keys"] = ["k"]
        with patch.object(
            resolution.material, "search_videos_pexels", return_value=[_stock_item("123")]
        ):
            candidate = resolution.search_stock(source="pexels", search_term="phone repair")[0]

        cache_file = os.path.join(self.storage, "cached.mp4")
        with open(cache_file, "wb") as handle:
            handle.write(b"video-bytes")
        with patch.object(resolution.material, "save_video", return_value=cache_file) as save:
            first = resolution.import_stock(candidate["candidate_id"])
            second = resolution.import_stock(candidate["candidate_id"])
        save.assert_called_once()
        self.assertEqual(first["file"], "stock-pexels-123-77.mp4")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertTrue(os.path.exists(self.local_file(first["file"])))

        for bad in ("https://evil.example/x.mp4", "../../etc/passwd", "ABC"):
            with self.assertRaises(resolution.MaterialResolutionError):
                resolution.import_stock(bad)
        with self.assertRaises(resolution.MaterialNotFoundError):
            resolution.import_stock("0" * 32)


class TestPaidGenerationJobs(_StorageMixin, unittest.TestCase):
    def _generator(self, generate, configured=True, unconfirmed=(), known=()):
        return resolution._Generator(
            "video", lambda: configured, generate, unconfirmed, known
        )

    def _start(self, **overrides):
        kwargs = dict(
            client_request_id="scene-0001-attempt-1",
            source="wavespeed",
            prompt="Vietnamese technician repairing a phone",
            video_aspect="9:16",
            duration=5,
            paid_cost_approved=True,
            run_inline=True,
        )
        kwargs.update(overrides)
        return resolution.start_generation(**kwargs)

    def test_requires_paid_approval_and_makes_no_call_without_it(self):
        generate = MagicMock()
        with patch.dict(resolution._GENERATORS, {"wavespeed": self._generator(generate)}):
            with self.assertRaises(resolution.PaidApprovalRequiredError):
                self._start(paid_cost_approved=False)
        generate.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.storage, "material_jobs", "scene-0001-attempt-1.json")))

    def test_success_writes_material_and_replay_adopts_without_second_paid_call(self):
        item = MaterialInfo(provider="wavespeed", url="https://cdn.example/out.mp4", duration=5)
        item.source_info = {"asset_id": "pred-42"}
        generate = MagicMock(return_value=[item])

        def fake_save(url, save_dir, provider):
            target = os.path.join(save_dir, "vid.mp4")
            with open(target, "wb") as handle:
                handle.write(b"generated")
            return target

        with patch.dict(resolution._GENERATORS, {"wavespeed": self._generator(generate)}), patch.object(
            resolution.material, "_save_generated_video_with_retry", side_effect=fake_save
        ):
            first = self._start()
            # Replay without approval: must adopt, never call the provider again.
            replay = self._start(paid_cost_approved=False)
        generate.assert_called_once()
        self.assertEqual(first["status"], "succeeded")
        self.assertFalse(first["adopted"])
        self.assertTrue(replay["adopted"])
        self.assertEqual(replay["file"], "ai-wavespeed-scene-0001-attempt-1.mp4")
        self.assertEqual(replay["remote_task_id"], "pred-42")
        self.assertTrue(os.path.exists(self.local_file(replay["file"])))
        self.assertNotIn("prompt", replay)

    def test_unconfirmed_task_is_never_resubmitted(self):
        generate = MagicMock(
            side_effect=resolution.material.WaveSpeedUnconfirmedTaskError("timeout", "pred-9")
        )
        gen = self._generator(
            generate, unconfirmed=(resolution.material.WaveSpeedUnconfirmedTaskError,)
        )
        with patch.dict(resolution._GENERATORS, {"wavespeed": gen}):
            first = self._start()
            again = self._start()
            status = resolution.get_generation("scene-0001-attempt-1")
        generate.assert_called_once()
        self.assertEqual(first["status"], "unconfirmed")
        self.assertEqual(first["remote_task_id"], "pred-9")
        self.assertTrue(again["adopted"])
        self.assertEqual(status["status"], "unconfirmed")

    def test_interrupted_submitting_job_becomes_unconfirmed(self):
        jobs = os.path.join(self.storage, "material_jobs")
        os.makedirs(jobs, exist_ok=True)
        record = {
            "client_request_id": "scene-0002-attempt-1",
            "source": "wavespeed",
            "status": "submitting",
            "fingerprint": resolution._fingerprint("wavespeed", "p", "9:16", 5),
            "owner": resolution._process_owner,
            "created_at": 1.0,
        }
        with open(os.path.join(jobs, "scene-0002-attempt-1.json"), "w") as handle:
            json.dump(record, handle)
        generate = MagicMock()
        with patch.dict(resolution._GENERATORS, {"wavespeed": self._generator(generate)}):
            adopted = self._start(client_request_id="scene-0002-attempt-1", prompt="p")
        generate.assert_not_called()
        self.assertEqual(adopted["status"], "unconfirmed")

    def test_same_id_with_different_request_conflicts(self):
        generate = MagicMock(return_value=[])
        with patch.dict(resolution._GENERATORS, {"wavespeed": self._generator(generate)}):
            self._start()
            with self.assertRaises(resolution.MaterialConflictError):
                self._start(prompt="a different scene")
        generate.assert_called_once()

    def test_rejects_unsafe_ids_and_unconfigured_provider(self):
        generate = MagicMock()
        with patch.dict(resolution._GENERATORS, {"wavespeed": self._generator(generate, configured=False)}):
            for bad in ("../../etc/passwd", "short", "a/b/c/d/e/f/g/h"):
                with self.assertRaises(resolution.MaterialResolutionError):
                    self._start(client_request_id=bad)
            with self.assertRaises(resolution.MaterialConflictError):
                self._start()
        generate.assert_not_called()


class TestMaterialRoutesHTTP(_StorageMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(asgi.app)

    def test_routes_require_api_key(self):
        config.app["api_key"] = "engine-secret"
        for method, path, body in (
            ("post", "/api/v1/materials/search", {"source": "pexels", "search_term": "x"}),
            ("post", "/api/v1/materials/import", {"candidate_id": "0" * 32}),
            (
                "post",
                "/api/v1/materials/generate",
                {"client_request_id": "abcdefgh", "source": "wavespeed", "prompt": "p"},
            ),
            ("get", "/api/v1/materials/generate/abcdefgh", None),
        ):
            response = getattr(self.client, method)(path, **({"json": body} if body else {}))
            self.assertEqual(response.status_code, 401, path)

    def test_generate_without_approval_is_402(self):
        generate = MagicMock()
        with patch.dict(
            resolution._GENERATORS,
            {"wavespeed": resolution._Generator("video", lambda: True, generate, (), ())},
        ):
            response = self.client.post(
                "/api/v1/materials/generate",
                json={"client_request_id": "abcdefgh", "source": "wavespeed", "prompt": "p"},
            )
        self.assertEqual(response.status_code, 402)
        generate.assert_not_called()

    def test_bad_ids_are_400_and_unknown_is_404(self):
        self.assertEqual(
            self.client.post("/api/v1/materials/import", json={"candidate_id": "../x"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/v1/materials/generate/" + "a" * 20).status_code, 404
        )

    def test_existing_upload_route_rejects_unsupported_files(self):
        response = self.client.post(
            "/api/v1/video_materials",
            files={"file": ("../../evil.sh", b"#!/bin/sh", "text/plain")},
        )
        self.assertEqual(response.status_code, 400)


class TestOrderedSceneAssembly(unittest.TestCase):
    def _params(self, materials, **extra):
        return VideoParams(
            video_subject="s",
            video_script="Kịch bản cố định.",
            video_source="local",
            video_materials=materials,
            use_material_durations=True,
            **extra,
        )

    def test_default_is_legacy(self):
        self.assertFalse(VideoParams(video_subject="s").use_material_durations)

    def test_missing_material_fails_instead_of_shifting_scenes(self):
        materials = [
            MaterialInfo(provider="local", url="a.mp4", duration=3),
            MaterialInfo(provider="local", url="missing.mp4", duration=4),
        ]
        params = self._params(materials)
        with patch.object(
            task_service.video, "preprocess_video", return_value=[materials[0]]
        ) as preprocess, patch.object(task_service, "_mark_task_failed") as failed:
            result = task_service.get_video_materials("t-1", params, "", 7)
        self.assertIsNone(result)
        self.assertTrue(preprocess.call_args.kwargs["use_material_durations"])
        self.assertIn("ordered materials", failed.call_args.args[2])

    def test_manifest_records_scene_provider_per_material(self):
        materials = [
            MaterialInfo(
                provider="local",
                url="shop.mp4",
                duration=3,
                source_info={"scene_id": "s1", "original_provider": "user_media", "api_key": "X"},
            ),
            MaterialInfo(
                provider="local",
                url="stock-pexels-1-2.mp4",
                duration=4,
                source_info={"scene_id": "s2", "original_provider": "pexels"},
            ),
        ]
        params = self._params(materials)
        with patch.object(
            task_service.video, "preprocess_video", side_effect=lambda **kw: kw["materials"]
        ), patch.object(task_service.task_artifacts, "patch_script_data") as patch_data:
            paths = task_service.get_video_materials("t-1", params, "", 7)
        self.assertEqual(paths, ["shop.mp4", "stock-pexels-1-2.mp4"])
        manifest = patch_data.call_args.kwargs["ordered_materials"]
        self.assertEqual([row["source_info"]["scene_id"] for row in manifest], ["s1", "s2"])
        self.assertEqual(manifest[1]["source_info"]["original_provider"], "pexels")
        self.assertNotIn("api_key", manifest[0]["source_info"])

    def test_final_video_uses_sequential_order_and_per_material_durations(self):
        materials = [
            MaterialInfo(provider="local", url="a.mp4", duration=3),
            MaterialInfo(provider="local", url="b.mp4", duration=0),
        ]
        params = self._params(materials, video_count=2, video_clip_duration=5)
        with patch.object(task_service.video, "combine_videos") as combine, patch.object(
            task_service.video, "generate_video", return_value=True
        ), patch.object(task_service.sm.state, "update_task"), patch.object(
            task_service.utils, "task_dir", return_value=tempfile.gettempdir()
        ):
            task_service.generate_final_videos(
                "t-1", params, ["a.mp4", "b.mp4"], "voice.mp3", "", 8
            )
        kwargs = combine.call_args.kwargs
        self.assertEqual(kwargs["video_concat_mode"].value, "sequential")
        self.assertEqual(kwargs["clip_durations"], [3.0, 5.0])
        self.assertNotIn("source_usage", kwargs)

    def test_combine_videos_cuts_each_material_to_its_own_duration(self):
        class _Audio:
            duration = 9.0

            def close(self):
                pass

        class _Clip:
            def __init__(self, duration):
                self.duration = duration
                self.size = (1080, 1920)
                self.w, self.h = self.size

            def subclipped(self, start, end):
                return _Clip(end - start)

        durations = {"a.mp4": 10.0, "b.mp4": 10.0, "c.mp4": 10.0}
        written = []
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            vd, "AudioFileClip", return_value=_Audio()
        ), patch.object(
            vd, "_open_video_clip_quietly", side_effect=lambda p: _Clip(durations[p])
        ), patch.object(
            vd,
            "_write_videofile_with_codec_fallback",
            side_effect=lambda clip, *a, **k: written.append(clip.duration),
        ), patch.object(vd, "concat_video_clips_with_ffmpeg") as concat, patch.object(
            vd, "delete_files"
        ):
            vd.combine_videos(
                combined_video_path=os.path.join(temp_dir, "combined.mp4"),
                video_paths=["a.mp4", "b.mp4", "c.mp4"],
                audio_file="voice.mp3",
                video_concat_mode=vd.VideoConcatMode.sequential,
                max_clip_duration=5,
                clip_durations=[2.0, 4.0, 3.5],
            )
        self.assertEqual(written[:3], [2.0, 4.0, 3.5])
        clip_files = concat.call_args.kwargs["clip_files"]
        self.assertTrue(clip_files[0].endswith("temp-clip-1.mp4"))


if __name__ == "__main__":
    unittest.main()

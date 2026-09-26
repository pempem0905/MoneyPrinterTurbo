"""Read-only engine capability report for server-to-server integrations.

``GET /api/v1/capabilities`` tells an integrating app (Common OS) which material
sources and render options this engine supports, and which sources are actually
configured, WITHOUT ever returning a credential. Every ``configured`` flag is a
boolean derived from the running config; no key, token, base URL or raw
``config.toml`` value is ever serialized.

The endpoint is intentionally read-only. Secrets stay in ``config.toml`` /
environment variables and are managed through the existing, allowlisted
``/api/v1/provider-config`` route only.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import Depends

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.schema import (
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoTransitionMode,
    _SUBTITLE_ANIMATIONS,
    _SUBTITLE_DISPLAY_MODES,
)
from app.services import (
    elevenlabs_music,
    loomloom,
    material,
    material_upload,
    metaso_minimax,
    ofox,
    sonilo,
    twelvelabs,
    volcengine_seedance,
)
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])

CAPABILITIES_CONTRACT_VERSION = 1

_ALL_ASPECTS = [aspect.value for aspect in VideoAspect]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return any(_has_value(item) for item in value)
    return True


def _config_key_configured(key: str) -> Callable[[], bool]:
    return lambda: _has_value(config.app.get(key))


def _safe_check(check: Callable[[], bool]) -> bool:
    """A misconfigured provider must report ``False``, never break the endpoint."""
    try:
        return bool(check())
    except Exception:
        return False


def _local_materials_count() -> int:
    try:
        local_dir = utils.storage_dir("local_videos", create=True)
        extensions = {
            ext.lower().lstrip(".")
            for ext in material_upload.SUPPORTED_MATERIAL_EXTENSIONS
        }
        return sum(
            1
            for name in os.listdir(local_dir)
            if name.rsplit(".", 1)[-1].lower() in extensions
            and os.path.isfile(os.path.join(local_dir, name))
        )
    except Exception:
        return 0


# Static per-source facts. ``configured`` is evaluated at request time.
# ``api_renderable`` is False when the source cannot run from a plain
# ``POST /api/v1/videos`` request (LoomLoom requires an interactive paid quote).
_SOURCES: list[dict[str, Any]] = [
    {
        "id": "local",
        "label": "Local / uploaded materials",
        "type": "local",
        "paid": False,
        "configured": lambda: True,
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": False,
        "supports_direct_material": True,
        "cost_warning": "",
    },
    {
        "id": "pexels",
        "label": "Pexels",
        "type": "stock",
        "paid": False,
        "configured": _config_key_configured("pexels_api_keys"),
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": False,
        "supports_direct_material": False,
        "cost_warning": "",
    },
    {
        "id": "pixabay",
        "label": "Pixabay",
        "type": "stock",
        "paid": False,
        "configured": _config_key_configured("pixabay_api_keys"),
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": False,
        "supports_direct_material": False,
        "cost_warning": "",
    },
    {
        "id": "coverr",
        "label": "Coverr",
        "type": "stock",
        "paid": False,
        "configured": _config_key_configured("coverr_api_keys"),
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": False,
        "supports_direct_material": False,
        "cost_warning": "",
    },
    {
        "id": "wavespeed",
        "label": "WaveSpeed text-to-video",
        "type": "ai_video",
        "paid": True,
        "configured": _config_key_configured("wavespeed_api_keys"),
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": True,
        "supports_direct_material": False,
        "cost_warning": "Billed per generated clip by WaveSpeed.",
    },
    {
        "id": "volcengine_seedance",
        "label": "Volcano Engine Seedance",
        "type": "ai_video",
        "paid": True,
        "configured": volcengine_seedance.is_enabled,
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": True,
        "supports_direct_material": False,
        "cost_warning": "Billed per generated clip by Volcano Engine Ark.",
    },
    {
        "id": "ofox",
        "label": "OFox text-to-video",
        "type": "ai_video",
        "paid": True,
        "configured": ofox.is_enabled,
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": True,
        "supports_direct_material": False,
        "cost_warning": "Billed per generated clip by OFox.",
    },
    {
        "id": "metaso_minimax",
        "label": "Metaso MiniMax video",
        "type": "ai_video",
        "paid": True,
        "configured": metaso_minimax.is_enabled,
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": True,
        "supports_direct_material": False,
        "cost_warning": "Billed per generated clip by Metaso.",
    },
    {
        "id": "loomloom",
        "label": "LoomLoom video",
        "type": "ai_video",
        "paid": True,
        "configured": lambda: bool(loomloom.resolve_api_token(config.app)),
        "api_renderable": False,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": True,
        "supports_direct_material": False,
        "cost_warning": (
            "Billed per run; requires an interactive confirmed quote and cannot "
            "be started from POST /api/v1/videos."
        ),
    },
    {
        "id": "openai_image",
        "label": "OpenAI-compatible image generation",
        "type": "ai_image",
        "paid": True,
        "configured": lambda: material.is_openai_image_enabled(),
        "api_renderable": True,
        "aspects": _ALL_ASPECTS,
        "supports_scene_prompt": True,
        "supports_direct_material": False,
        "cost_warning": "Billed per generated image by the configured gateway.",
    },
]

_MUSIC_PROVIDERS: list[dict[str, Any]] = [
    {"id": "sonilo", "paid": True, "configured": sonilo.is_enabled},
    {"id": "elevenlabs", "paid": True, "configured": elevenlabs_music.is_enabled},
]


def build_capabilities() -> dict[str, Any]:
    """Build the public capability report. Contains booleans and enums only."""
    sources = []
    for spec in _SOURCES:
        aspects = spec["aspects"]
        sources.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "type": spec["type"],
                "paid": spec["paid"],
                "configured": _safe_check(spec["configured"]),
                "api_renderable": spec["api_renderable"],
                "supports_portrait": VideoAspect.portrait.value in aspects,
                "supports_landscape": VideoAspect.landscape.value in aspects,
                "supports_square": VideoAspect.square.value in aspects,
                "supports_scene_prompt": spec["supports_scene_prompt"],
                "supports_direct_material": spec["supports_direct_material"],
                "cost_warning": spec["cost_warning"],
            }
        )

    return {
        "contract_version": CAPABILITIES_CONTRACT_VERSION,
        "sources": sources,
        "music_providers": [
            {
                "id": spec["id"],
                "paid": spec["paid"],
                "configured": _safe_check(spec["configured"]),
            }
            for spec in _MUSIC_PROVIDERS
        ],
        "render_options": {
            "video_aspect": _ALL_ASPECTS,
            "video_fit_mode": [mode.value for mode in VideoFitMode],
            "video_concat_mode": [mode.value for mode in VideoConcatMode],
            "video_transition_mode": [
                mode.value for mode in VideoTransitionMode if mode.value is not None
            ],
            "video_clip_duration": {"min": 1},
            "video_clip_speed": {
                "min": utils._CLIP_SPEED_MIN,
                "max": utils._CLIP_SPEED_MAX,
            },
            "video_count": {"min": 1},
            "paragraph_number": {"min": 1, "max": 10},
            "subtitle_position": [
                "bottom",
                "top",
                "center",
                "two_thirds_bottom",
                "custom",
            ],
            "subtitle_display_mode": list(_SUBTITLE_DISPLAY_MODES),
            "subtitle_animation": list(_SUBTITLE_ANIMATIONS),
            "bgm_type": ["", "random", "sonilo", "elevenlabs"],
            "video_music_prompt": {"max_length": 2000},
        },
        "script_contract": {
            # A non-empty video_script is used verbatim (task.generate_script).
            "exact_script_preserved": True,
            # Non-empty video_terms are used verbatim (task.generate_terms); the
            # only possible change is a reorder by TwelveLabs when it is enabled
            # and match_materials_to_script is False.
            "exact_terms_preserved": True,
            "terms_may_be_reordered": _safe_check(twelvelabs.is_enabled),
        },
        "direct_materials": {
            # Explicit materials are honored only with video_source="local".
            # Each entry's url is a file name inside the engine's local_videos
            # directory (upload with POST /api/v1/video_materials first).
            "video_source": "local",
            "upload_endpoint": "/api/v1/video_materials",
            "list_endpoint": "/api/v1/video_materials",
            "mixed_provider_urls": False,
            "sequential_assembly": True,
            # Ordered scene assembly: set use_material_durations=true to use every
            # material exactly once, in order, cut to its own duration.
            "ordered_scene_assembly": True,
            "per_material_duration_param": "use_material_durations",
            "stock_search_endpoint": "/api/v1/materials/search",
            "stock_import_endpoint": "/api/v1/materials/import",
            "generation_endpoint": "/api/v1/materials/generate",
            "local_materials_count": _local_materials_count(),
        },
    }


@router.get("/capabilities", summary="Read-only engine capability report")
def get_capabilities():
    return {"status": 200, "message": "success", "data": build_capabilities()}

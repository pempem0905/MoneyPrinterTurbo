from __future__ import annotations

from typing import Any

from fastapi import Depends
from pydantic import BaseModel

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router

router = new_router(dependencies=[Depends(base.verify_token)])


# Only these runtime configuration keys may be changed through the mobile API.
# Keeping an explicit allowlist prevents arbitrary config.toml writes.
APP_FIELDS = {
    # selection / general
    "video_source",
    "llm_provider",
    "subtitle_provider",
    # stock materials
    "pexels_api_keys",
    "pixabay_api_keys",
    "coverr_api_keys",
    # AI video / image materials
    "wavespeed_api_keys",
    "wavespeed_text_to_video_model",
    "volcengine_seedance_api_key",
    "volcengine_seedance_base_url",
    "volcengine_seedance_model",
    "volcengine_seedance_resolution",
    "ofox_api_key",
    "ofox_base_url",
    "ofox_text_to_video_model",
    "ofox_resolution",
    "ofox_provider",
    "metaso_minimax_api_key",
    "metaso_minimax_base_url",
    "metaso_minimax_resolution",
    "openai_image_base_url",
    "openai_image_api_keys",
    "openai_image_model",
    "openai_image_size",
    "openai_image_prompt_template",
    "loomloom_api_token",
    "loomloom_base_url",
    # music
    "sonilo_api_key",
    "sonilo_base_url",
    # LLMs / gateways
    "moonshot_api_key",
    "moonshot_base_url",
    "moonshot_model_name",
    "shengsuanyun_api_key",
    "shengsuanyun_base_url",
    "shengsuanyun_model_name",
    "apimart_api_key",
    "apimart_base_url",
    "apimart_model_name",
    "openai_api_key",
    "openai_base_url",
    "openai_model_name",
    "anthropic_api_key",
    "anthropic_base_url",
    "anthropic_model_name",
    "gemini_api_key",
    "gemini_model_name",
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model_name",
    "qwen_api_key",
    "qwen_model_name",
    "azure_api_key",
    "azure_base_url",
    "azure_model_name",
    "azure_api_version",
    "volcengine_api_key",
    "volcengine_base_url",
    "volcengine_model_name",
    "grok_api_key",
    "grok_base_url",
    "grok_model_name",
    "minimax_api_key",
    "minimax_base_url",
    "minimax_model_name",
    "mimo_api_key",
    "mimo_base_url",
    "mimo_model_name",
    "cloudflare_api_key",
    "cloudflare_account_id",
    "cloudflare_gateway_id",
    "cloudflare_model_name",
    "modelscope_api_key",
    "modelscope_base_url",
    "modelscope_model_name",
    "aihubmix_api_key",
    "aihubmix_base_url",
    "aihubmix_model_name",
    "aimlapi_api_key",
    "aimlapi_base_url",
    "aimlapi_model_name",
    "evolink_api_key",
    "evolink_base_url",
    "evolink_model_name",
    "openrouter_api_key",
    "openrouter_base_url",
    "openrouter_model_name",
    "ollama_base_url",
    "ollama_model_name",
    "claude_code_model_name",
    "claude_code_cli_path",
    "claude_code_timeout",
    "oneapi_api_key",
    "oneapi_base_url",
    "oneapi_model_name",
    "litellm_model_name",
    "groq_api_key",
    "groq_base_url",
    "groq_model_name",
    "pollinations_api_key",
    "pollinations_base_url",
    "pollinations_model_name",
    # MiMo TTS shares MiMo credentials
    "mimo_tts_model_name",
    "mimo_tts_style_prompt",
    # Upload-Post
    "upload_post_enabled",
    "upload_post_api_key",
    "upload_post_username",
    "upload_post_platforms",
    "upload_post_auto_upload",
    "upload_post_youtube_privacy_status",
    "upload_post_youtube_made_for_kids",
}

SECTION_FIELDS = {
    "app": APP_FIELDS,
    "azure": {"speech_key", "speech_region"},
    "siliconflow": {"api_key"},
    "minimax_tts": {
        "api_key",
        "base_url",
        "model_id",
        "voice_id",
        "sample_rate",
        "bitrate",
        "audio_format",
        "channel",
        "pitch",
    },
    "elevenlabs": {"api_key", "model_id", "music_model_id", "music_timeout"},
    "chatterbox": {"base_url", "api_key", "model_id", "voices"},
    "kokoro": {"base_url", "api_key", "model_id", "voices"},
    "fish_audio": {"api_key", "model", "voices"},
}

SECTION_OBJECTS = {
    "app": config.app,
    "azure": config.azure,
    "siliconflow": config.siliconflow,
    "minimax_tts": config.minimax_tts,
    "elevenlabs": config.elevenlabs,
    "chatterbox": config.chatterbox,
    "kokoro": config.kokoro,
    "fish_audio": config.fish_audio,
}

EXPLICIT_SECRET_KEYS = {
    "speech_key",
    "redis_password",
}


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return (
        key in EXPLICIT_SECRET_KEYS
        or lowered.endswith("api_key")
        or lowered.endswith("api_keys")
        or lowered.endswith("api_token")
        or lowered.endswith("password")
    )


def _configured(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _mask(value: Any) -> str:
    if not _configured(value):
        return ""
    if isinstance(value, list):
        return f"Đã lưu {len(value)} key" if len(value) != 1 else "Đã lưu 1 key"
    text = str(value)
    if len(text) <= 4:
        return "••••"
    return f"••••{text[-4:]}"


def _safe_value(key: str, value: Any) -> Any:
    if _is_secret(key):
        return None
    return value


def _validate_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(
        item is None or isinstance(item, (str, int, float, bool)) for item in value
    ):
        return value
    raise ValueError("provider config values must be JSON primitives or lists")


class ProviderConfigPatch(BaseModel):
    updates: dict[str, dict[str, Any]]


@router.get("/provider-config")
def get_provider_config():
    values: dict[str, dict[str, Any]] = {}
    configured: dict[str, bool] = {}
    masked: dict[str, str] = {}

    for section_name, allowed_fields in SECTION_FIELDS.items():
        section = SECTION_OBJECTS[section_name]
        section_values: dict[str, Any] = {}
        for key in sorted(allowed_fields):
            value = section.get(key)
            section_values[key] = _safe_value(key, value)
            if _is_secret(key):
                flat_key = f"{section_name}.{key}"
                configured[flat_key] = _configured(value)
                masked[flat_key] = _mask(value)
        values[section_name] = section_values

    return {
        "status": 200,
        "message": "success",
        "data": {
            "values": values,
            "configured": configured,
            "masked": masked,
        },
    }


@router.patch("/provider-config")
def patch_provider_config(payload: ProviderConfigPatch):
    changed: list[str] = []

    for section_name, updates in payload.updates.items():
        if section_name not in SECTION_FIELDS:
            raise ValueError(f"unsupported provider config section: {section_name}")

        section = SECTION_OBJECTS[section_name]
        allowed_fields = SECTION_FIELDS[section_name]
        for key, raw_value in updates.items():
            if key not in allowed_fields:
                raise ValueError(f"unsupported provider config key: {section_name}.{key}")

            value = _validate_value(raw_value)

            # Secret inputs use null to mean "leave unchanged". Empty string/list is
            # an explicit clear operation; this lets the UI edit non-secret fields
            # without ever reading a stored credential back from the server.
            if _is_secret(key) and value is None:
                continue

            config.update_config_nonblocking(section, key, value)
            changed.append(f"{section_name}.{key}")

    config.try_save_config()

    return {
        "status": 200,
        "message": "saved",
        "data": {"changed": changed},
    }


@router.get("/provider-catalog")
def get_provider_catalog():
    return {
        "status": 200,
        "message": "success",
        "data": {
            "stock": ["pexels", "pixabay", "coverr", "local"],
            "ai_materials": [
                "wavespeed",
                "volcengine_seedance",
                "ofox",
                "metaso_minimax",
                "loomloom",
                "openai_image",
            ],
            "llm": [
                "moonshot",
                "shengsuanyun",
                "apimart",
                "openai",
                "anthropic",
                "gemini",
                "deepseek",
                "qwen",
                "azure",
                "volcengine",
                "grok",
                "minimax",
                "mimo",
                "cloudflare",
                "modelscope",
                "aihubmix",
                "aimlapi",
                "evolink",
                "openrouter",
                "ollama",
                "claude_code",
                "oneapi",
                "litellm",
                "groq",
                "pollinations",
            ],
            "tts": [
                "edge",
                "azure",
                "siliconflow",
                "mimo",
                "minimax",
                "elevenlabs",
                "chatterbox",
                "kokoro",
                "fish_audio",
            ],
            "music": ["none", "random", "preset", "upload", "sonilo", "elevenlabs"],
            "publish": ["upload-post"],
        },
    }

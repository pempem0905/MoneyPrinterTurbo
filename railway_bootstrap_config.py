import base64
import binascii
import json
import os
import shutil
from pathlib import Path

import toml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.toml"
EXAMPLE = ROOT / "config.example.toml"


def _read_keys(name: str) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    except Exception:
        pass
    try:
        value = toml.loads(f"value = {raw}")["value"]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
    except Exception:
        pass
    parts = raw.replace("\n", ",").split(",")
    return [part.strip().strip('"').strip("'") for part in parts if part.strip().strip('"').strip("'")]


def main() -> None:
    source = "config_example"
    encoded = (os.getenv("MPT_BOOTSTRAP_CONFIG_B64") or "").strip()
    if encoded:
        try:
            CONFIG.write_bytes(base64.b64decode(encoded, validate=True))
            # Validate the decoded payload before trusting it.
            toml.load(CONFIG)
            source = "full_config_b64"
        except (binascii.Error, ValueError, OSError, toml.TomlDecodeError):
            shutil.copyfile(EXAMPLE, CONFIG)
            source = "config_example_fallback"
    else:
        shutil.copyfile(EXAMPLE, CONFIG)

    cfg = toml.load(CONFIG)
    app = cfg.setdefault("app", {})

    provider_envs = {
        "pexels_api_keys": "MPT_BOOTSTRAP_PEXELS_API_KEYS",
        "pixabay_api_keys": "MPT_BOOTSTRAP_PIXABAY_API_KEYS",
        "coverr_api_keys": "MPT_BOOTSTRAP_COVERR_API_KEYS",
    }
    provider_counts: dict[str, int] = {}
    for config_key, env_name in provider_envs.items():
        keys = _read_keys(env_name)
        if keys:
            app[config_key] = keys
        provider_counts[config_key] = len(app.get(config_key) or [])

    api_key = (os.getenv("MPT_API_KEY") or "").strip()
    if api_key:
        app["api_key"] = api_key

    with CONFIG.open("w", encoding="utf-8") as fh:
        toml.dump(cfg, fh)

    counts = " ".join(f"{key}={value}" for key, value in provider_counts.items())
    print(f"STAGING_CONFIG_READY source={source} {counts} auth={'on' if bool(app.get('api_key')) else 'off'}")


if __name__ == "__main__":
    main()

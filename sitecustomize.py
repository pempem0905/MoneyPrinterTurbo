"""Cloud bootstrap helpers loaded automatically by Python.

Railway mounts the durable MoneyPrinterTurbo config at ``MPT_CONFIG_FILE``.
The upstream config writer replaces ``config.toml`` atomically with ``os.replace``;
a symlink therefore gets replaced after the first save and later changes stop
reaching the volume.  Keep a normal local config file and mirror every completed
save back to the mounted file instead.

A one-time bootstrap can restore credentials that were entered before this fix.
Bootstrap values come only from Railway environment variables and are never
committed to the repository.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path


def _atomic_copy(source: Path, target: Path) -> None:
    """Copy a complete file into place without exposing a partial TOML."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.sync.tmp")
    shutil.copyfile(source, temp)
    os.replace(temp, target)


def _split_keys(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _apply_one_time_bootstrap(target: Path, example: Path) -> None:
    """Seed the persistent config once from Railway-only recovery variables."""
    version = os.getenv("MPT_BOOTSTRAP_VERSION", "").strip()
    if not version:
        return

    marker = target.parent / f".mpt-bootstrap-{version}.done"
    if marker.exists():
        return

    try:
        import toml
    except Exception:
        return

    if target.is_file():
        cfg = toml.load(target)
    elif example.is_file():
        cfg = toml.load(example)
    else:
        cfg = {}

    app = cfg.setdefault("app", {})

    provider = os.getenv("MPT_BOOTSTRAP_LLM_PROVIDER", "").strip()
    if provider:
        app["llm_provider"] = provider

    gemini_model = os.getenv("MPT_BOOTSTRAP_GEMINI_MODEL", "").strip()
    if gemini_model:
        app["gemini_model_name"] = gemini_model

    scalar_env_map = {
        "MPT_BOOTSTRAP_GEMINI_API_KEY": "gemini_api_key",
        "MPT_BOOTSTRAP_OPENROUTER_API_KEY": "openrouter_api_key",
        "MPT_BOOTSTRAP_UPLOAD_POST_API_KEY": "upload_post_api_key",
    }
    for env_name, config_key in scalar_env_map.items():
        value = os.getenv(env_name, "").strip()
        if value:
            app[config_key] = value

    list_env_map = {
        "MPT_BOOTSTRAP_PEXELS_API_KEYS": "pexels_api_keys",
        "MPT_BOOTSTRAP_PIXABAY_API_KEYS": "pixabay_api_keys",
        "MPT_BOOTSTRAP_COVERR_API_KEYS": "coverr_api_keys",
    }
    for env_name, config_key in list_env_map.items():
        values = _split_keys(os.getenv(env_name, ""))
        if values:
            app[config_key] = values

    # Write directly inside the volume so the seed survives the deploy that
    # installs this fix.
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.bootstrap.tmp")
    with temp.open("w", encoding="utf-8") as fp:
        toml.dump(cfg, fp)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, target)
    marker.write_text("ok\n", encoding="utf-8")


def _sync_loop(local_config: Path, persistent_config: Path) -> None:
    """Mirror upstream atomic saves from the container into the Railway volume."""
    last_signature: tuple[int, int] | None = None
    while True:
        try:
            if local_config.is_file() and not local_config.is_symlink():
                stat = local_config.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature != last_signature:
                    _atomic_copy(local_config, persistent_config)
                    last_signature = signature
        except Exception:
            # Persistence must never prevent the video engine from starting or
            # rendering. A later polling pass will retry the copy.
            pass
        time.sleep(0.75)


def _prepare_persistent_config() -> None:
    configured = os.getenv("MPT_CONFIG_FILE", "").strip()
    if not configured:
        return

    root = Path(__file__).resolve().parent
    target = Path(configured)
    example = root / "config.example.toml"
    local_config = root / "config.toml"

    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        if example.is_file():
            shutil.copyfile(example, target)
        else:
            target.touch()

    _apply_one_time_bootstrap(target, example)

    # Never leave a symlink here: upstream save_config() uses os.replace(), which
    # replaces the symlink itself and silently disconnects future writes from the
    # mounted volume.
    if local_config.is_symlink() or local_config.exists():
        local_config.unlink()
    shutil.copyfile(target, local_config)

    threading.Thread(
        target=_sync_loop,
        args=(local_config, target),
        name="mpt-config-volume-sync",
        daemon=True,
    ).start()


_prepare_persistent_config()

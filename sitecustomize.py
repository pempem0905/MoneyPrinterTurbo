"""Cloud bootstrap helpers loaded automatically by Python.

When MPT_CONFIG_FILE is set, make the repository-level config.toml point at that
persistent file before MoneyPrinterTurbo imports app.config. This keeps the
upstream config loader unchanged while allowing Railway to mount durable storage.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _link_persistent_config() -> None:
    configured = os.getenv("MPT_CONFIG_FILE", "").strip()
    if not configured:
        return

    root = Path(__file__).resolve().parent
    target = Path(configured)
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        example = root / "config.example.toml"
        if example.is_file():
            shutil.copyfile(example, target)
        else:
            target.touch()

    local_config = root / "config.toml"
    try:
        if local_config.is_symlink() or local_config.exists():
            local_config.unlink()
        local_config.symlink_to(target)
    except OSError:
        # If the filesystem does not support symlinks, copy the persistent file
        # into place. Normal Railway/Linux deployments use the symlink path.
        shutil.copyfile(target, local_config)


_link_persistent_config()

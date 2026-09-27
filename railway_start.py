"""Railway staging launcher for MoneyPrinterTurbo.

Runs the staging config bootstrap and then replaces the process with engine.py.
No shell chaining is used, so Railway command parsing cannot drop the engine step.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _probe_pexels_once() -> None:
    """STAGING diagnostic: verify Pexels auth without logging credentials."""
    try:
        import requests
        import toml

        config_path = ROOT / "config.toml"
        cfg = toml.load(config_path)
        keys = (cfg.get("app") or {}).get("pexels_api_keys") or []
        key = str(keys[0]).strip() if isinstance(keys, list) and keys else ""
        if not key:
            print("PEXELS_PROBE status=NO_KEY", flush=True)
            return

        response = requests.get(
            "https://api.pexels.com/v1/videos/search",
            params={"query": "smartphone", "per_page": 1, "orientation": "portrait"},
            headers={"Authorization": key},
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError:
            data = None
        top_keys = sorted(str(k) for k in data.keys())[:10] if isinstance(data, dict) else []
        message = ""
        if isinstance(data, dict):
            for field in ("message", "error", "detail"):
                value = data.get(field)
                if isinstance(value, str) and value.strip():
                    message = value.strip()[:160]
                    break
        print(
            "PEXELS_PROBE "
            f"status={response.status_code} "
            f"content_type={response.headers.get('content-type', '')} "
            f"auth_nonempty={bool(key)} "
            f"keys={top_keys} "
            f"message={message or 'none'}",
            flush=True,
        )
    except Exception as exc:
        print(f"PEXELS_PROBE error={type(exc).__name__}", flush=True)


def main() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "railway_bootstrap_config.py")],
        check=True,
        cwd=ROOT,
    )
    _probe_pexels_once()
    print("RAILWAY_START_ENGINE", flush=True)
    os.chdir(ROOT)
    os.execv(
        sys.executable,
        [sys.executable, "-u", str(ROOT / "engine.py")],
    )


if __name__ == "__main__":
    main()

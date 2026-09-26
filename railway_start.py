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


def main() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "railway_bootstrap_config.py")],
        check=True,
        cwd=ROOT,
    )
    print("RAILWAY_START_ENGINE", flush=True)
    os.chdir(ROOT)
    os.execv(
        sys.executable,
        [sys.executable, "-u", str(ROOT / "engine.py")],
    )


if __name__ == "__main__":
    main()

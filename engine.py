"""API-only entrypoint for our MoneyPrinterTurbo video engine.

This wrapper intentionally leaves the upstream generation pipeline untouched.
It applies production-safe runtime overrides from environment variables before
FastAPI is imported by Uvicorn.
"""

import os

import uvicorn
from loguru import logger

from app.config import config


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def configure_runtime() -> None:
    """Apply engine-only settings without writing secrets into config.toml."""
    api_key = os.getenv("MPT_API_KEY", "").strip()
    allow_insecure = _env_flag("MPT_ALLOW_INSECURE", False)

    if not api_key and not allow_insecure:
        raise RuntimeError(
            "MPT_API_KEY is required for engine mode. "
            "Set MPT_ALLOW_INSECURE=1 only for trusted local development."
        )

    if api_key:
        config.app["api_key"] = api_key

    config.listen_host = os.getenv("MPT_LISTEN_HOST", "0.0.0.0").strip() or "0.0.0.0"

    raw_port = os.getenv("MPT_LISTEN_PORT", "8080").strip()
    try:
        listen_port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("MPT_LISTEN_PORT must be an integer") from exc
    if not 1 <= listen_port <= 65535:
        raise RuntimeError("MPT_LISTEN_PORT must be between 1 and 65535")
    config.listen_port = listen_port


if __name__ == "__main__":
    configure_runtime()
    logger.info(
        "start API-only video engine, host={}, port={}, auth={}",
        config.listen_host,
        config.listen_port,
        "enabled" if config.app.get("api_key") else "disabled",
    )
    uvicorn.run(
        app="app.asgi:app",
        host=config.listen_host,
        port=config.listen_port,
        reload=False,
        log_level="warning",
    )

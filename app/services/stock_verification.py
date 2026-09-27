"""Safe, non-billed verification of the engine's stock-source API keys.

``verify_stock_source`` performs ONE minimal search request per configured key
against the provider's free search API (Pexels / Pixabay / Coverr), never
downloads media and never returns or logs the key. The result is a small
classification Common OS can show verbatim:

* ``ok``             - every configured key was accepted
* ``invalid_key``    - at least one key was rejected (HTTP 400/401/403)
* ``rate_limited``   - the provider throttled the check (HTTP 429)
* ``unreachable``    - network error, timeout, 5xx, Cloudflare challenge or a
                       response that is not the provider's JSON
* ``not_configured`` - no key is stored for this source
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config
from app.services import material

STOCK_VERIFY_SOURCES = ("pexels", "pixabay", "coverr")
_KEY_FIELDS = {
    "pexels": "pexels_api_keys",
    "pixabay": "pixabay_api_keys",
    "coverr": "coverr_api_keys",
}
# A generic, always-populated term; the check only proves the key is accepted.
_PROBE_TERM = "nature"
_MAX_KEYS_CHECKED = 5
_TIMEOUT = (10, 20)

STATUS_OK = "ok"
STATUS_INVALID_KEY = "invalid_key"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_UNREACHABLE = "unreachable"
STATUS_NOT_CONFIGURED = "not_configured"


class UnsupportedStockSourceError(ValueError):
    """Source is not a verifiable stock provider (maps to HTTP 400)."""


def _configured_keys(source: str) -> list[str]:
    raw = config.app.get(_KEY_FIELDS[source])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if isinstance(item, str) and item.strip()]


def _request_pexels(key: str) -> requests.Response:
    query = urlencode({"query": _PROBE_TERM, "per_page": 1})
    return requests.get(
        f"https://api.pexels.com/videos/search?{query}",
        headers={"Authorization": key, "User-Agent": "MoneyPrinterTurbo"},
        proxies=config.proxy,
        verify=material._get_tls_verify(),
        timeout=_TIMEOUT,
    )


def _request_pixabay(key: str) -> requests.Response:
    # Pixabay's minimum page size is 3.
    query = urlencode({"key": key, "q": _PROBE_TERM, "per_page": 3})
    return requests.get(
        f"https://pixabay.com/api/videos/?{query}",
        proxies=config.proxy,
        verify=material._get_tls_verify(),
        timeout=_TIMEOUT,
    )


def _request_coverr(key: str) -> requests.Response:
    query = urlencode({"query": _PROBE_TERM, "page_size": 1})
    return requests.get(
        f"https://api.coverr.co/videos?{query}",
        headers={"Authorization": f"Bearer {key}"},
        proxies=config.proxy,
        verify=material._get_tls_verify(),
        timeout=_TIMEOUT,
    )


_REQUESTS: dict[str, Callable[[str], requests.Response]] = {
    "pexels": _request_pexels,
    "pixabay": _request_pixabay,
    "coverr": _request_coverr,
}
_EXPECTED_FIELD = {"pexels": "videos", "pixabay": "hits", "coverr": "hits"}


def _classify(source: str, response: Any) -> tuple[str, int | None]:
    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in (400, 401, 403):
        if material._is_cloudflare_challenge(response):
            return STATUS_UNREACHABLE, status_code
        return STATUS_INVALID_KEY, status_code
    if status_code == 429:
        return STATUS_RATE_LIMITED, status_code
    if status_code != 200:
        return STATUS_UNREACHABLE, status_code or None
    try:
        body = response.json()
    except ValueError:
        return STATUS_UNREACHABLE, status_code
    if not isinstance(body, dict) or _EXPECTED_FIELD[source] not in body:
        return STATUS_UNREACHABLE, status_code
    return STATUS_OK, status_code


# Worst outcome wins: one rejected key makes the whole source "invalid_key".
_SEVERITY = [STATUS_OK, STATUS_RATE_LIMITED, STATUS_UNREACHABLE, STATUS_INVALID_KEY]


def verify_stock_source(source: str) -> dict[str, Any]:
    if source not in STOCK_VERIFY_SOURCES:
        raise UnsupportedStockSourceError(f"unsupported stock source: {source}")
    keys = _configured_keys(source)
    if not keys:
        return {
            "source": source,
            "status": STATUS_NOT_CONFIGURED,
            "keys_configured": 0,
            "keys_checked": 0,
            "keys_ok": 0,
            "http_status": None,
        }

    checked = keys[:_MAX_KEYS_CHECKED]
    worst = STATUS_OK
    worst_http: int | None = None
    keys_ok = 0
    for key in checked:
        try:
            status, http_status = _classify(source, _REQUESTS[source](key))
        except requests.RequestException as exc:
            # Only the exception type is logged: request errors can echo the URL
            # (Pixabay passes the key as a query parameter).
            logger.warning(f"{source} key verification failed: {type(exc).__name__}")
            status, http_status = STATUS_UNREACHABLE, None
        if status == STATUS_OK:
            keys_ok += 1
        if _SEVERITY.index(status) > _SEVERITY.index(worst):
            worst, worst_http = status, http_status
        elif status == worst and worst_http is None:
            worst_http = http_status

    logger.info(f"{source} key verification: status={worst}, ok={keys_ok}/{len(checked)}")
    return {
        "source": source,
        "status": worst,
        "keys_configured": len(keys),
        "keys_checked": len(checked),
        "keys_ok": keys_ok,
        "http_status": worst_http,
    }

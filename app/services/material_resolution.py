"""Per-scene material resolution for server-to-server callers (Common OS).

Three primitives, all writing into the engine's ``local_videos`` directory so
the result can be referenced from ``video_materials`` with
``video_source="local"``:

* ``search_stock``  - search Pexels/Pixabay/Coverr with the engine's own keys.
  Candidate download URLs are kept in a server-side registry and never
  returned; callers only receive an opaque ``candidate_id``. This keeps the
  engine from ever downloading a caller-supplied URL (no SSRF surface).
* ``import_stock``  - download one registered candidate into ``local_videos``
  under a deterministic name. Importing the same candidate twice returns the
  existing file (idempotent).
* ``start_generation`` / ``get_generation`` - one paid AI clip/image per
  caller-supplied ``client_request_id``. The job record is claimed atomically
  BEFORE any paid call. A second request with the same id adopts the existing
  record and never submits another paid task, whatever its status
  (including ``unconfirmed`` and interrupted jobs).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models.schema import VideoAspect
from app.services import material, metaso_minimax, ofox, volcengine_seedance
from app.utils import utils

STOCK_SOURCES = ("pexels", "pixabay", "coverr")
MAX_SEARCH_RESULTS = 20
MAX_PROMPT_LENGTH = 2000
MAX_GENERATION_DURATION = 30
_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_CANDIDATE_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]+")

JOB_SUBMITTING = "submitting"
JOB_SUCCEEDED = "succeeded"
JOB_UNCONFIRMED = "unconfirmed"
JOB_DOWNLOAD_FAILED = "download_failed"
JOB_FAILED = "failed"

# A job still "submitting" after this long cannot be trusted to finish.
_STALE_SUBMITTING_SECONDS = 2 * 60 * 60

_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mpt-material-gen")
_active_lock = threading.RLock()
_active_jobs: set[str] = set()
_record_lock = threading.RLock()


class MaterialResolutionError(ValueError):
    """Invalid request (maps to HTTP 400)."""


class MaterialNotFoundError(LookupError):
    """Unknown candidate / job (maps to HTTP 404)."""


class MaterialConflictError(RuntimeError):
    """Request conflicts with existing state (maps to HTTP 409)."""


class PaidApprovalRequiredError(PermissionError):
    """Paid generation without explicit approval (maps to HTTP 402)."""


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------


def _local_videos_dir() -> str:
    return utils.storage_dir("local_videos", create=True)


def _candidates_dir() -> str:
    return utils.storage_dir("material_candidates", create=True)


def _jobs_dir() -> str:
    return utils.storage_dir("material_jobs", create=True)


def _write_json_atomic(target: str, payload: dict) -> None:
    directory = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _read_json(target: str) -> dict | None:
    try:
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None


def _safe_segment(value: Any, fallback: str) -> str:
    text = _SAFE_SEGMENT.sub("-", str(value or "")).strip("-")[:48]
    return text or fallback


def _aspect(value: Any) -> VideoAspect:
    try:
        return VideoAspect(value or VideoAspect.portrait.value)
    except ValueError as exc:
        raise MaterialResolutionError(f"unsupported video_aspect: {value}") from exc


def _public_source_info(source_info: Any) -> dict[str, Any]:
    """Whitelist the attribution fields that are safe to return / persist."""
    info = source_info if isinstance(source_info, dict) else {}
    out: dict[str, Any] = {}
    asset_id = info.get("asset_id")
    if asset_id not in (None, ""):
        out["asset_id"] = str(asset_id)
    source_page = material._safe_public_url(info.get("source_page"))
    if source_page:
        out["source_page"] = source_page
    creator = material._creator_info(info.get("creator"))
    if creator:
        out["creator"] = creator
    rendition = info.get("rendition")
    if isinstance(rendition, dict):
        for field in ("width", "height"):
            value = rendition.get(field)
            if isinstance(value, (int, float)) and value > 0:
                out[field] = int(value)
        if rendition.get("id") not in (None, ""):
            out["rendition_id"] = str(rendition.get("id"))
    return out


# --------------------------------------------------------------------------
# stock search / import
# --------------------------------------------------------------------------

_STOCK_SEARCHERS: dict[str, Callable[..., list]] = {
    "pexels": lambda **kw: material.search_videos_pexels(**kw),
    "pixabay": lambda **kw: material.search_videos_pixabay(**kw),
    "coverr": lambda **kw: material.search_videos_coverr(**kw),
}
_STOCK_KEYS = {
    "pexels": "pexels_api_keys",
    "pixabay": "pixabay_api_keys",
    "coverr": "coverr_api_keys",
}


def _candidate_id(provider: str, url: str, info: dict) -> str:
    identity = "|".join(
        [provider, str(info.get("asset_id") or ""), str(info.get("rendition_id") or ""), url.split("?")[0]]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def search_stock(
    *,
    source: str,
    search_term: str,
    video_aspect: Any = VideoAspect.portrait.value,
    minimum_duration: int = 1,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if source not in STOCK_SOURCES:
        raise MaterialResolutionError(f"unsupported stock source: {source}")
    term = str(search_term or "").strip()
    if not term or len(term) > 200:
        raise MaterialResolutionError("search_term must be 1-200 characters")
    if not config.app.get(_STOCK_KEYS[source]):
        raise MaterialConflictError(f"{source} is not configured on this engine")
    aspect = _aspect(video_aspect)
    try:
        minimum = max(1, int(minimum_duration))
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    except (TypeError, ValueError) as exc:
        raise MaterialResolutionError("minimum_duration/limit must be integers") from exc

    items = _STOCK_SEARCHERS[source](
        search_term=term, minimum_duration=minimum, video_aspect=aspect
    )
    candidates: list[dict[str, Any]] = []
    for item in items or []:
        url = str(getattr(item, "url", "") or "")
        if not url.startswith(("http://", "https://")):
            continue
        public = _public_source_info(getattr(item, "source_info", None))
        candidate_id = _candidate_id(source, url, public)
        candidate = {
            "candidate_id": candidate_id,
            "provider": source,
            "search_term": term,
            "duration": int(getattr(item, "duration", 0) or 0),
            "width": public.get("width"),
            "height": public.get("height"),
            "video_aspect": aspect.value,
            **{k: v for k, v in public.items() if k not in ("width", "height")},
        }
        _write_json_atomic(
            os.path.join(_candidates_dir(), f"{candidate_id}.json"),
            {**candidate, "download_url": url},
        )
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _imported_file_name(candidate: dict) -> str:
    provider = _safe_segment(candidate.get("provider"), "stock")
    asset = _safe_segment(candidate.get("asset_id"), candidate["candidate_id"][:12])
    rendition = _safe_segment(candidate.get("rendition_id"), "r")
    return f"stock-{provider}-{asset}-{rendition}.mp4"


def import_stock(candidate_id: str) -> dict[str, Any]:
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.match(candidate_id):
        raise MaterialResolutionError("invalid candidate_id")
    record = _read_json(os.path.join(_candidates_dir(), f"{candidate_id}.json"))
    if not record:
        raise MaterialNotFoundError("unknown candidate_id; search again first")
    public = {k: v for k, v in record.items() if k != "download_url"}
    file_name = _imported_file_name(record)
    target = os.path.join(_local_videos_dir(), file_name)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return {**public, "file": file_name, "material_type": "video", "reused": True}

    cached = material.save_video(
        video_url=record["download_url"], save_dir=utils.storage_dir("cache_videos", create=True)
    )
    if not cached:
        raise MaterialConflictError("stock material could not be downloaded or is not a valid video")
    fd, tmp = tempfile.mkstemp(prefix=".import-", suffix=".mp4", dir=_local_videos_dir())
    os.close(fd)
    try:
        shutil.copyfile(cached, tmp)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {**public, "file": file_name, "material_type": "video", "reused": False}


# --------------------------------------------------------------------------
# paid AI generation jobs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Generator:
    material_type: str  # "video" | "image"
    is_configured: Callable[[], bool]
    generate: Callable[..., list]
    unconfirmed_errors: tuple[type[BaseException], ...]
    known_errors: tuple[type[BaseException], ...]


def _remote_id(exc: BaseException) -> str:
    return str(getattr(exc, "task_id", "") or getattr(exc, "prediction_id", "") or "").strip()


_GENERATORS: dict[str, _Generator] = {
    "wavespeed": _Generator(
        "video",
        lambda: bool(config.app.get("wavespeed_api_keys")),
        lambda **kw: material.generate_videos_wavespeed(**kw),
        (material.WaveSpeedUnconfirmedTaskError,),
        (),
    ),
    "volcengine_seedance": _Generator(
        "video",
        volcengine_seedance.is_enabled,
        lambda **kw: volcengine_seedance.generate_videos(**kw),
        (volcengine_seedance.VolcEngineSeedanceUnconfirmedTaskError,),
        (volcengine_seedance.VolcEngineSeedanceError,),
    ),
    "ofox": _Generator(
        "video",
        ofox.is_enabled,
        lambda **kw: ofox.generate_videos(**kw),
        (ofox.OFoxUnconfirmedTaskError,),
        (ofox.OFoxError,),
    ),
    "metaso_minimax": _Generator(
        "video",
        metaso_minimax.is_enabled,
        lambda **kw: metaso_minimax.generate_videos(**kw),
        (metaso_minimax.MetasoMiniMaxUnconfirmedTaskError,),
        (metaso_minimax.MetasoMiniMaxError,),
    ),
    "openai_image": _Generator(
        "image",
        lambda: material.is_openai_image_enabled(),
        lambda **kw: material.generate_images_openai(**kw),
        (),
        (),
    ),
}


def _job_path(client_request_id: str) -> str:
    return os.path.join(_jobs_dir(), f"{client_request_id}.json")


def _fingerprint(source: str, prompt: str, aspect: str, duration: int) -> str:
    raw = json.dumps([source, prompt, aspect, duration], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_job(record: dict) -> dict[str, Any]:
    keys = (
        "client_request_id",
        "source",
        "status",
        "material_type",
        "file",
        "duration",
        "video_aspect",
        "remote_task_id",
        "error",
        "created_at",
        "updated_at",
        "source_info",
    )
    return {key: record.get(key) for key in keys if key in record}


def _update_job(client_request_id: str, **fields: Any) -> dict:
    with _record_lock:
        record = _read_json(_job_path(client_request_id)) or {}
        record.update(fields)
        record["updated_at"] = time.time()
        _write_json_atomic(_job_path(client_request_id), record)
        return record


def _is_active(client_request_id: str) -> bool:
    with _active_lock:
        return client_request_id in _active_jobs


def _reconcile(record: dict) -> dict:
    """A 'submitting' job no longer running here is unconfirmed, never retried."""
    if record.get("status") != JOB_SUBMITTING or _is_active(record["client_request_id"]):
        return record
    interrupted_here = record.get("owner") == _process_owner
    stale = time.time() - float(record.get("created_at") or 0) > _STALE_SUBMITTING_SECONDS
    if interrupted_here or stale:
        return _update_job(
            record["client_request_id"],
            status=JOB_UNCONFIRMED,
            error="generation was interrupted before completion; check the provider console",
        )
    return record


def get_generation(client_request_id: str) -> dict[str, Any]:
    if not isinstance(client_request_id, str) or not _CLIENT_REQUEST_ID.match(client_request_id):
        raise MaterialResolutionError("invalid client_request_id")
    record = _read_json(_job_path(client_request_id))
    if not record:
        raise MaterialNotFoundError("unknown client_request_id")
    return _public_job(_reconcile(record))


def start_generation(
    *,
    client_request_id: str,
    source: str,
    prompt: str,
    video_aspect: Any = VideoAspect.portrait.value,
    duration: int = 5,
    paid_cost_approved: bool = False,
    run_inline: bool = False,
) -> dict[str, Any]:
    """Claim and start one paid generation. Replays adopt the existing job."""
    if not isinstance(client_request_id, str) or not _CLIENT_REQUEST_ID.match(client_request_id):
        raise MaterialResolutionError("client_request_id must match [A-Za-z0-9_-]{8,128}")
    generator = _GENERATORS.get(source)
    if generator is None:
        raise MaterialResolutionError(f"unsupported generation source: {source}")
    text = str(prompt or "").strip()
    if not text or len(text) > MAX_PROMPT_LENGTH:
        raise MaterialResolutionError(f"prompt must be 1-{MAX_PROMPT_LENGTH} characters")
    aspect = _aspect(video_aspect)
    try:
        seconds = int(duration)
    except (TypeError, ValueError) as exc:
        raise MaterialResolutionError("duration must be an integer") from exc
    if not 1 <= seconds <= MAX_GENERATION_DURATION:
        raise MaterialResolutionError(f"duration must be 1-{MAX_GENERATION_DURATION}")
    fingerprint = _fingerprint(source, text, aspect.value, seconds)

    # Adopt an existing job first: a replay must never reach a paid call, and
    # must not even require approval again.
    existing = _read_json(_job_path(client_request_id))
    if existing:
        if existing.get("fingerprint") != fingerprint:
            raise MaterialConflictError(
                "client_request_id already used for a different generation request"
            )
        return {**_public_job(_reconcile(existing)), "adopted": True}

    if paid_cost_approved is not True:
        raise PaidApprovalRequiredError("paid generation requires paid_cost_approved=true")
    try:
        configured = bool(generator.is_configured())
    except Exception:
        configured = False
    if not configured:
        raise MaterialConflictError(f"{source} is not configured on this engine")

    now = time.time()
    record = {
        "client_request_id": client_request_id,
        "source": source,
        "status": JOB_SUBMITTING,
        "material_type": generator.material_type,
        "duration": seconds,
        "video_aspect": aspect.value,
        "prompt": text,
        "fingerprint": fingerprint,
        "owner": _process_owner,
        "created_at": now,
        "updated_at": now,
    }
    # Atomic claim: O_EXCL guarantees one winner across threads/processes
    # sharing the storage directory, BEFORE any paid request is made.
    # Mark active before the record becomes visible so a concurrent status read
    # never mistakes the fresh claim for an interrupted job.
    with _active_lock:
        already_active = client_request_id in _active_jobs
        _active_jobs.add(client_request_id)
    try:
        fd = os.open(_job_path(client_request_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not already_active:
            with _active_lock:
                _active_jobs.discard(client_request_id)
        existing = _read_json(_job_path(client_request_id)) or {}
        return {**_public_job(existing), "adopted": True}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False)
    if run_inline:
        _run_generation(client_request_id, generator)
    else:
        _executor.submit(_run_generation, client_request_id, generator)
    return {**_public_job(_read_json(_job_path(client_request_id)) or record), "adopted": False}


def _run_generation(client_request_id: str, generator: _Generator) -> None:
    record = _read_json(_job_path(client_request_id)) or {}
    source = record.get("source", "")
    aspect = VideoAspect(record.get("video_aspect") or VideoAspect.portrait.value)
    seconds = int(record.get("duration") or 5)
    work_dir = tempfile.mkdtemp(prefix=f"gen-{client_request_id}-", dir=_jobs_dir())
    try:
        try:
            if generator.material_type == "image":
                items = generator.generate(
                    search_term=record["prompt"],
                    minimum_duration=seconds,
                    video_aspect=aspect,
                    save_dir=work_dir,
                )
            else:
                items = generator.generate(
                    search_term=record["prompt"],
                    minimum_duration=seconds,
                    video_aspect=aspect,
                )
        except generator.unconfirmed_errors as exc:
            _update_job(
                client_request_id,
                status=JOB_UNCONFIRMED,
                remote_task_id=_remote_id(exc),
                error=str(exc)[:500],
            )
            return
        except generator.known_errors as exc:
            remote = _remote_id(exc)
            # A known error that carries a remote id may still have been billed.
            _update_job(
                client_request_id,
                status=JOB_UNCONFIRMED if remote else JOB_FAILED,
                remote_task_id=remote,
                error=str(exc)[:500],
            )
            return

        item = (items or [None])[0]
        if item is None:
            _update_job(client_request_id, status=JOB_FAILED, error=f"{source} returned no material")
            return
        public = _public_source_info(getattr(item, "source_info", None))
        remote = public.get("asset_id", "")
        if generator.material_type == "image":
            produced = str(item.url or "")
            extension = os.path.splitext(produced)[1].lower() or ".png"
        else:
            produced = material._save_generated_video_with_retry(item.url, work_dir, source)
            extension = ".mp4"
        if not produced or not os.path.exists(produced):
            _update_job(
                client_request_id,
                status=JOB_DOWNLOAD_FAILED,
                remote_task_id=remote,
                error="paid result could not be downloaded; recover it from the provider console",
            )
            return
        file_name = f"ai-{_safe_segment(source, 'ai')}-{client_request_id}{extension}"
        os.replace(produced, os.path.join(_local_videos_dir(), file_name))
        _update_job(
            client_request_id,
            status=JOB_SUCCEEDED,
            file=file_name,
            remote_task_id=remote,
            source_info={k: v for k, v in public.items() if k != "asset_id"},
        )
    except Exception as exc:  # never leave a paid job in "submitting"
        logger.exception(f"material generation crashed: id={client_request_id}")
        _update_job(
            client_request_id,
            status=JOB_UNCONFIRMED,
            error=f"generation crashed: {type(exc).__name__}",
        )
    finally:
        with _active_lock:
            _active_jobs.discard(client_request_id)
        shutil.rmtree(work_dir, ignore_errors=True)

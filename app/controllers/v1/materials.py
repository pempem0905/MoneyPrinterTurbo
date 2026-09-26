"""Scene material resolution API (server-to-server, x-api-key protected).

Routes (all under ``/api/v1``):

* ``POST /materials/search``   - stock candidates (no download URLs returned)
* ``POST /materials/import``   - import one candidate into ``local_videos``
* ``POST /materials/generate`` - claim/adopt one paid AI generation job
* ``GET  /materials/generate/{client_request_id}`` - job status

Uploading caller-owned media (shop footage) reuses the existing
``POST /api/v1/video_materials`` route. Every returned ``file`` is a bare file
name usable as ``video_materials[].url`` with ``video_source="local"``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request
from pydantic import BaseModel, Field

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services import material_resolution as resolution
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


class MaterialSearchRequest(BaseModel):
    source: str
    search_term: str = Field(max_length=200)
    video_aspect: Optional[str] = "9:16"
    minimum_duration: int = Field(default=1, ge=1, le=120)
    limit: int = Field(default=10, ge=1, le=resolution.MAX_SEARCH_RESULTS)


class MaterialImportRequest(BaseModel):
    candidate_id: str = Field(max_length=64)


class MaterialGenerateRequest(BaseModel):
    client_request_id: str = Field(max_length=128)
    source: str
    prompt: str = Field(max_length=resolution.MAX_PROMPT_LENGTH)
    video_aspect: Optional[str] = "9:16"
    duration: int = Field(default=5, ge=1, le=resolution.MAX_GENERATION_DURATION)
    paid_cost_approved: bool = False


def _call(request: Request, fn, *args, **kwargs):
    request_id = base.get_task_id(request)
    try:
        return utils.get_response(200, fn(*args, **kwargs))
    except resolution.MaterialResolutionError as exc:
        status = 400
        message = str(exc)
    except resolution.PaidApprovalRequiredError as exc:
        status = 402
        message = str(exc)
    except resolution.MaterialNotFoundError as exc:
        status = 404
        message = str(exc.args[0] if exc.args else "not found")
    except resolution.MaterialConflictError as exc:
        status = 409
        message = str(exc)
    raise HttpException(task_id=request_id, status_code=status, message=f"{request_id}: {message}")


@router.post("/materials/search", summary="Search stock candidates for one scene")
def search_materials(request: Request, body: MaterialSearchRequest):
    return _call(
        request,
        lambda: {
            "candidates": resolution.search_stock(
                source=body.source,
                search_term=body.search_term,
                video_aspect=body.video_aspect,
                minimum_duration=body.minimum_duration,
                limit=body.limit,
            )
        },
    )


@router.post("/materials/import", summary="Import one stock candidate into local_videos")
def import_material(request: Request, body: MaterialImportRequest):
    return _call(request, resolution.import_stock, body.candidate_id)


@router.post("/materials/generate", summary="Start or adopt one paid AI material generation")
def generate_material(request: Request, body: MaterialGenerateRequest):
    return _call(
        request,
        resolution.start_generation,
        client_request_id=body.client_request_id,
        source=body.source,
        prompt=body.prompt,
        video_aspect=body.video_aspect,
        duration=body.duration,
        paid_cost_approved=body.paid_cost_approved,
    )


@router.get("/materials/generate/{client_request_id}", summary="Paid generation job status")
def get_generated_material(request: Request, client_request_id: str):
    return _call(request, resolution.get_generation, client_request_id)

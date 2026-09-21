from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import Field

from calliope.production import Camera, ProductionModel, ProductionStore, World

router = APIRouter()


class WorldEdit(ProductionModel):
    expected_revision: int = Field(ge=0)
    world: World


class CameraEdit(ProductionModel):
    expected_revision: int = Field(ge=0)
    camera: Camera
    end_camera: Camera | None = None
    frame_end: int | None = Field(default=None, ge=1, le=172800)
    rough_board_id: str | None = None


class PrevisRequest(ProductionModel):
    expected_revision: int = Field(ge=0)
    clip_ids: list[int] = Field(min_length=1)
    mode: Literal["stills", "animation"] = "stills"


class BoardSource(ProductionModel):
    expected_revision: int = Field(ge=0)
    clip_id: int = Field(gt=0)
    stage: Literal["rough", "refined", "styled"]
    source_board_id: str | None = None
    previs_job_id: int | None = Field(default=None, gt=0)
    previs_frame: int | None = Field(default=None, ge=1)


class BoardGeneration(BoardSource):
    workflow_id: int = Field(gt=0)
    input_values: dict[str, str | int | float] = Field(default_factory=dict)


class BoardImport(BoardSource):
    generation_job_id: int | None = Field(default=None, gt=0)
    output_index: int = Field(default=0, ge=0)
    note: str = Field(default="", max_length=4000)


class BoardSelection(ProductionModel):
    expected_revision: int = Field(ge=0)
    board_id: str


class VideoGeneration(ProductionModel):
    expected_revision: int = Field(ge=0)
    clip_id: int = Field(gt=0)
    workflow_id: int = Field(gt=0)
    prompt: str = Field(min_length=1, max_length=64000)
    duration_seconds: float = Field(ge=5, le=15, allow_inf_nan=False)
    seed: int = Field(ge=0, le=2**64 - 1)
    styled_board_id: str
    previs_job_id: int | None = Field(default=None, gt=0)
    reference_paths: dict[str, str] = Field(default_factory=dict)


class VideoSelection(ProductionModel):
    expected_revision: int = Field(ge=0)
    generation_job_id: int = Field(gt=0)
    output_index: int = Field(ge=0)


@router.post("/{project_id}/production/videos/select")
def select_video(project_id: int, payload: VideoSelection):
    try:
        return ProductionStore(project_id).select_video_output(**payload.model_dump())
    except (ValueError, OSError) as exc:
        raise _error(ValueError(str(exc))) from exc


@router.post("/{project_id}/production/videos/generate")
def generate_video(project_id: int, payload: VideoGeneration):
    from calliope.routers.jobs import _job_public
    from calliope.video_production import enqueue_video

    try:
        return _job_public(enqueue_video(project_id, **payload.model_dump()))
    except (ValueError, OSError, TimeoutError) as exc:
        raise _error(ValueError(str(exc))) from exc


@router.post("/{project_id}/production/boards/generate")
def generate_board(project_id: int, payload: BoardGeneration):
    from calliope.routers.jobs import _job_public
    from calliope.storyboards import enqueue_board

    try:
        return _job_public(enqueue_board(project_id, **payload.model_dump()))
    except (ValueError, OSError) as exc:
        raise _error(ValueError(str(exc))) from exc


@router.post("/{project_id}/production/boards/register")
def register_board(project_id: int, payload: BoardImport):
    from calliope.storyboards import register_image

    try:
        return register_image(project_id, **payload.model_dump())
    except (ValueError, OSError) as exc:
        raise _error(ValueError(str(exc))) from exc


@router.post("/{project_id}/production/boards/upload")
async def upload_board(project_id: int, metadata: str = Form(...), file: UploadFile = File(...)):
    from calliope.storyboards import register_image

    try:
        payload = BoardImport.model_validate_json(metadata)
        if payload.generation_job_id is not None:
            raise ValueError("Uploads cannot claim to be a generated job output")
        data = await file.read(32 * 1024 * 1024 + 1)
        return register_image(project_id, data=data, **payload.model_dump())
    except (ValueError, OSError) as exc:
        raise _error(ValueError(str(exc))) from exc
    finally:
        await file.close()


@router.post("/{project_id}/production/boards/select")
def select_board(project_id: int, payload: BoardSelection):
    try:
        return ProductionStore(project_id).select_board(**payload.model_dump())
    except (ValueError, OSError) as exc:
        raise _error(ValueError(str(exc))) from exc


@router.post("/{project_id}/production/previs")
def render_previs(project_id: int, payload: PrevisRequest):
    from calliope.previs import enqueue_previs
    from calliope.routers.jobs import _job_public

    try:
        return _job_public(enqueue_previs(project_id, **payload.model_dump()))
    except ValueError as exc:
        raise _error(exc) from exc


def _error(exc: ValueError) -> HTTPException:
    message = str(exc)
    code = 409 if "Production changed" in message else 400
    if "does not exist" in message or "does not belong" in message:
        code = 404
    return HTTPException(status_code=code, detail=message)


@router.get("/{project_id}/production")
def get_production(project_id: int):
    try:
        return ProductionStore(project_id).read()
    except ValueError as exc:
        raise _error(exc) from exc


@router.put("/{project_id}/production/world")
def set_world(project_id: int, payload: WorldEdit):
    try:
        return ProductionStore(project_id).set_world(
            **payload.model_dump(exclude={"world"}), world=payload.world
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.put("/{project_id}/production/shots/{clip_id}/camera")
def set_camera(project_id: int, clip_id: int, payload: CameraEdit):
    try:
        return ProductionStore(project_id).set_camera(
            clip_id,
            payload.camera,
            expected_revision=payload.expected_revision,
            end_camera=payload.end_camera,
            frame_end=payload.frame_end,
            rough_board_id=payload.rough_board_id,
        )
    except ValueError as exc:
        raise _error(exc) from exc

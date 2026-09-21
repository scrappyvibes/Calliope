"""Shot-scoped storyboard provenance and image-generation requests."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from pathlib import Path

from PIL import Image

from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.patcher import patch_workflow
from calliope.config import settings
from calliope.db import get_db
from calliope.production import ProductionStore, StoryboardTake
from calliope.queue.manager import queue_manager


def _job(project_id: int, job_id: int) -> dict:
    job = queue_manager.get_job(job_id)
    if not job or job["project_id"] != project_id or job["status"] != "done":
        raise ValueError("Source job must be completed in this project")
    return job


def _frame(project_id: int, clip_id: int, job_id: int, frame: int) -> tuple[str, str]:
    job = _job(project_id, job_id)
    if job["kind"] != "previs":
        raise ValueError("Source must be a Blender previs job")
    payload = json.loads(job["payload_json"])
    name = f"shot-{clip_id}-{frame:06d}.png"
    paths = json.loads(job["output_paths_json"])
    path = next((p for p in paths if Path(p).name == name), None)
    if not path or not Path(path).is_file():
        raise ValueError("That camera frame was not rendered by the source job")
    return path, payload["shot_hashes"][str(clip_id)]


def _provenance(
    project_id: int,
    clip_id: int,
    stage: str,
    *,
    source_board_id=None,
    previs_job_id=None,
    previs_frame=None,
) -> tuple[dict, str | None]:
    state = ProductionStore(project_id).read()
    parent = next((b for b in state["boards"] if b["id"] == source_board_id), None)
    if source_board_id and (not parent or parent["clip_id"] != clip_id):
        raise ValueError("Source board does not belong to this shot")
    provenance = {
        "source_board_id": source_board_id,
        "source_shot_hash": None,
        "previs_job_id": None,
        "previs_frame": None,
    }
    image = parent["path"] if parent else None
    if parent:
        if hashlib.sha256(Path(image).read_bytes()).hexdigest() != parent["sha256"]:
            raise ValueError("Source board image changed")
        provenance.update(
            {key: parent[key] for key in ("source_shot_hash", "previs_job_id", "previs_frame")}
        )
    if stage == "refined":
        if previs_job_id is None or previs_frame is None:
            raise ValueError("Refined boards require a Blender job and frame")
        image, digest = _frame(project_id, clip_id, previs_job_id, previs_frame)
        provenance.update(
            source_shot_hash=digest, previs_job_id=previs_job_id, previs_frame=previs_frame
        )
    elif stage == "styled":
        if not parent or parent["stage"] != "refined":
            raise ValueError("Styled boards require a refined source board")
    elif stage != "rough":
        raise ValueError("Invalid storyboard stage")
    return provenance, image


def register_image(
    project_id: int,
    *,
    expected_revision: int,
    clip_id: int,
    stage: str,
    data: bytes | None = None,
    generation_job_id: int | None = None,
    output_index: int = 0,
    note: str = "",
    **sources,
) -> dict:
    store = ProductionStore(project_id)
    if store.read()["revision"] != expected_revision:
        raise ValueError("Production changed; reload before saving a board")
    provenance, _ = _provenance(project_id, clip_id, stage, **sources)
    if generation_job_id is not None:
        job = _job(project_id, generation_job_id)
        target = json.loads(job["payload_json"]).get("storyboard_target") or {}
        if target.get("clip_id") != clip_id or target.get("stage") != stage:
            raise ValueError("Generation job is not for this shot and storyboard stage")
        if any(target.get(key) != provenance.get(key) for key in provenance):
            raise ValueError("Generation source does not match the requested board provenance")
        paths = json.loads(job["output_paths_json"])
        if output_index < 0 or output_index >= len(paths):
            raise ValueError("Invalid generated image output index")
        data = Path(paths[output_index]).read_bytes()
    if not data or len(data) > 32 * 1024 * 1024:
        raise ValueError("Board image must be between 1 byte and 32 MB")
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ("PNG", "JPEG", "WEBP"):
            raise ValueError("Board must be PNG, JPEG or WebP")
        extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
        image.verify()
    board_id = str(uuid.uuid4())
    path = settings.assets_dir / str(project_id) / "boards" / (board_id + extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    take = StoryboardTake(
        id=board_id,
        clip_id=clip_id,
        stage=stage,
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        generation_job_id=generation_job_id,
        note=note,
        **provenance,
    )
    try:
        return store.add_board(take, expected_revision=expected_revision)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def enqueue_board(
    project_id: int,
    *,
    expected_revision: int,
    clip_id: int,
    stage: str,
    workflow_id: int,
    input_values: dict,
    session_id: int | None = None,
    **sources,
) -> dict:
    state = ProductionStore(project_id).read()
    if state["revision"] != expected_revision:
        raise ValueError("Production changed; reload before generating a board")
    conn = get_db(settings.db_path)
    try:
        if not conn.execute(
            "SELECT id FROM clips WHERE project_id=? AND id=?", (project_id, clip_id)
        ).fetchone():
            raise ValueError("Clip does not belong to this project")
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    finally:
        conn.close()
    if not row or row["kind"] != "image" or not row["is_enabled"]:
        raise ValueError("Choose an enabled image workflow")
    graph = json.loads(row["workflow_json"])
    if stage == "styled" and not any(
        node.get("_meta", {}).get("calliope_adapter", {}).get("name")
        == "scrappyvibes_style_external_prompts"
        for node in graph.values()
    ):
        raise ValueError("Styled boards use the adapted GPT to Style workflow A")
    provenance, image = _provenance(project_id, clip_id, stage, **sources)
    shot = next((s for s in state["shots"] if s["clip_id"] == clip_id), None)
    if provenance["source_shot_hash"] and (
        not shot or provenance["source_shot_hash"] != shot["source_hash"]
    ):
        raise ValueError("Source image uses an earlier camera or world")
    inputs = parse_dynamic_inputs(graph)
    bindings = {str(i["nodeId"]): i for i in inputs}
    values = dict(input_values)
    if set(values) - set(bindings):
        raise ValueError("Unknown workflow input binding")
    if image:
        image_inputs = [i for i in inputs if i["kind"] == "image"]
        if len(image_inputs) != 1:
            raise ValueError("Storyboard workflow must expose exactly one source image input")
        values[str(image_inputs[0]["nodeId"])] = image
    references = []
    for key, binding in bindings.items():
        if binding["kind"] in ("image", "image_url", "video", "audio"):
            if key not in values or not values[key]:
                raise ValueError("Choose a project source image for this workflow")
            path = Path(str(values[key])).resolve()
            if not path.is_relative_to((settings.assets_dir / str(project_id)).resolve()):
                raise ValueError("Reference image must belong to this project")
            if not path.is_file():
                raise ValueError("Reference image no longer exists")
            references.append(
                {
                    "node_id": key,
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    return queue_manager.enqueue(
        project_id=project_id,
        clip_id=clip_id,
        kind="image",
        workflow_id=workflow_id,
        payload={
            "workflow_snapshot": patch_workflow(graph, values),
            "input_values": {},
            "session_id": session_id,
            "reference_manifest": references,
            "storyboard_target": {"clip_id": clip_id, "stage": stage, **provenance},
        },
    )

"""Freeze H3 shot inputs and their production lineage before queueing a take."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.patcher import patch_workflow
from calliope.config import settings
from calliope.db import get_db
from calliope.production import ProductionStore
from calliope.queue.manager import queue_manager


def _media(path: Path, kind: str) -> dict:
    if kind == "image":
        with Image.open(path) as image:
            image.verify()
        return {}
    binary = shutil.which("ffprobe")
    if not binary:
        raise ValueError("ffprobe is required to validate H3 video/audio references")
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-protocol_whitelist", "file,pipe",
             "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, timeout=30, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Reference media inspection timed out") from exc
    if result.returncode:
        raise ValueError("Could not inspect reference media")
    info = json.loads(result.stdout)
    if not any(s.get("codec_type") == kind for s in info.get("streams", [])):
        raise ValueError(f"Reference must contain a {kind} stream")
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not (2 <= duration <= 15):
        raise ValueError("H3 video/audio references must be 2–15 seconds long")
    return {"duration_seconds": duration}


def enqueue_video(
    project_id: int, *, expected_revision: int, clip_id: int, workflow_id: int,
    prompt: str, duration_seconds: float, seed: int, styled_board_id: str,
    previs_job_id: int | None = None, reference_paths: dict[str, str] | None = None,
    session_id: int | None = None,
) -> dict:
    state = ProductionStore(project_id).read()
    if state["revision"] != expected_revision:
        raise ValueError("Production changed; reload before generating a video")
    if not math.isfinite(duration_seconds) or not 5 <= duration_seconds <= 15:
        raise ValueError("H3 duration must be 5–15 seconds")
    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise ValueError("H3 seed must be an unsigned 64-bit integer")
    if not prompt.strip() or len(prompt) > 64000:
        raise ValueError("Enter an H3 prompt of 1–64000 characters")
    shot = next((s for s in state["shots"] if s["clip_id"] == clip_id), None)
    board = next((b for b in state["boards"] if b["id"] == styled_board_id), None)
    if not shot or not board or board["clip_id"] != clip_id or board["stage"] != "styled":
        raise ValueError("Choose a styled board belonging to this shot")
    if board["stale"] or board["source_shot_hash"] != shot["source_hash"]:
        raise ValueError("Styled board uses an earlier camera or world")
    conn = get_db(settings.db_path)
    try:
        clip = conn.execute("SELECT id FROM clips WHERE id=? AND project_id=?", (clip_id, project_id)).fetchone()
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    finally:
        conn.close()
    if not clip:
        raise ValueError("Clip does not belong to this project")
    if not row or row["kind"] != "video" or not row["is_enabled"]:
        raise ValueError("Choose an enabled H3 video workflow")
    graph = json.loads(row["workflow_json"])
    adapter = graph.get("339", {}).get("_meta", {}).get("calliope_adapter", {})
    if adapter.get("name") != "scrappyvibes_h3_references" or adapter.get("version") != 1:
        raise ValueError("Shot video generation requires adapted workflow B")
    inputs = {i["nodeId"]: i for i in parse_dynamic_inputs(graph)}
    refs = adapter.get("references", [])
    first_image = next((r for r in refs if r["label"] == "Picture 1"), None)
    first_video = next((r for r in refs if r["label"] == "Video 1"), None)
    if not first_image:
        raise ValueError("Workflow B must expose Picture 1 for the styled board")
    paths = dict(reference_paths or {})
    if first_image["node_id"] in paths or (first_video and first_video["node_id"] in paths):
        raise ValueError("Primary board and previs references cannot be overridden by paths")
    paths[first_image["node_id"]] = board["path"]
    if first_video:
        job = queue_manager.get_job(previs_job_id) if previs_job_id else None
        if not job or job["project_id"] != project_id or job["kind"] != "previs" or job["status"] != "done":
            raise ValueError("Choose a completed Blender motion job in this project")
        payload = json.loads(job["payload_json"])
        if payload.get("shot_hashes", {}).get(str(clip_id)) != shot["source_hash"]:
            raise ValueError("Blender motion uses an earlier camera or world")
        path = next((p for p in json.loads(job["output_paths_json"])
                     if Path(p).name == f"shot-{clip_id}.mp4"), None)
        if not path:
            raise ValueError("Blender job has no motion video for this shot")
        paths[first_video["node_id"]] = path
    elif previs_job_id is not None:
        raise ValueError("This workflow has no video reference socket")
    if set(paths) != {r["node_id"] for r in refs}:
        raise ValueError("Provide exactly the reference slots exposed by workflow B")
    labels = set(re.findall(r"<(Picture|Video|Audio) ([1-9][0-9]*)>", prompt))
    allowed = {tuple(r["label"].split(" ")) for r in refs}
    if labels - allowed:
        raise ValueError("H3 prompt mentions a reference label that is not bound")
    if ("Picture", "1") not in labels or (first_video and ("Video", "1") not in labels):
        raise ValueError("Describe the roles of <Picture 1> and any <Video 1> in the prompt")
    values = {"334": prompt, "259": duration_seconds, "256": seed}
    manifest = []
    root = (settings.assets_dir / str(project_id)).resolve()
    for ref in refs:
        key = ref["node_id"]
        if key not in inputs or inputs[key]["kind"] != ref["kind"]:
            raise ValueError("Workflow reference metadata does not match its input bindings")
        path = Path(paths[key]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Reference must be an existing asset in this project")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if key == first_image["node_id"] and digest != board["sha256"]:
            raise ValueError("Styled board image changed")
        metadata = _media(path, ref["kind"])
        values[key] = str(path)
        manifest.append({**ref, "path": str(path), "sha256": digest, **metadata})
    frames = max(5, round(duration_seconds * 24))
    frames += (5 - frames % 17) % 17
    return queue_manager.enqueue(
        project_id=project_id, clip_id=clip_id, kind="video", workflow_id=workflow_id,
        payload={"workflow_snapshot": patch_workflow(graph, values), "input_values": {},
                 "reference_manifest": manifest, "session_id": session_id,
                 "production_video_target": {
                     "clip_id": clip_id, "source_shot_hash": shot["source_hash"],
                     "production_revision": expected_revision, "styled_board_id": styled_board_id,
                     "previs_job_id": previs_job_id, "prompt": prompt, "seed": seed,
                     "requested_duration_seconds": duration_seconds, "frame_count": frames,
                     "fps": 24, "actual_duration_seconds": frames / 24,
                 }},
    )

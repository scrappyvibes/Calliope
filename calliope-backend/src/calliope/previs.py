"""Local Blender execution against an immutable production snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from calliope.comfyui.client import ComfyUIClient
from calliope.config import settings
from calliope.events.bus import event_bus
from calliope.production import ProductionDocument, ProductionStore, atomic_write, source_hash
from calliope.queue.manager import queue_manager


def blender_binary() -> str:
    configured = os.environ.get("CALLIOPE_BLENDER_BINARY")
    candidates = [
        configured,
        shutil.which("blender"),
        "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise ValueError("Blender not found. Set CALLIOPE_BLENDER_BINARY to its executable.")


def enqueue_previs(
    project_id: int,
    expected_revision: int,
    clip_ids: list[int],
    mode: str = "stills",
    session_id: int | None = None,
) -> dict:
    blender_binary()
    current = ProductionStore(project_id).read()
    if current["revision"] != expected_revision:
        raise ValueError("Production changed; reload before rendering")
    if mode not in ("stills", "animation"):
        raise ValueError("Invalid previs render mode")
    available = {shot["clip_id"] for shot in current["shots"]}
    if not clip_ids or len(set(clip_ids)) != len(clip_ids) or not set(clip_ids) <= available:
        raise ValueError("Every requested shot must have a saved camera in this project")
    # Remove derived hashes; only validated authored data enters Blender.
    document = {
        key: current[key] for key in ("format_version", "project_id", "revision", "world", "shots")
    }
    document["shots"] = [
        {k: v for k, v in shot.items() if k != "source_hash"} for shot in current["shots"]
    ]
    reference_ids = {s.get("rough_board_id") for s in current["shots"] if s["clip_id"] in clip_ids}
    document["boards"] = []
    for board in current["boards"]:
        if board["id"] not in reference_ids:
            continue
        path = Path(board["path"]).resolve()
        if not path.is_relative_to((settings.assets_dir / str(project_id)).resolve()) or not path.is_file():
            raise ValueError("Rough board must be an existing project image")
        if hashlib.sha256(path.read_bytes()).hexdigest() != board["sha256"]:
            raise ValueError("Rough board changed after it was recorded")
        document["boards"].append({k: v for k, v in board.items() if k != "stale"})
    document = ProductionDocument.model_validate(document).model_dump(mode="json")
    return queue_manager.enqueue(
        project_id=project_id,
        kind="previs",
        payload={
            "document": document,
            "source_hash": source_hash(document),
            "shot_hashes": {str(s["clip_id"]): s["source_hash"] for s in current["shots"]},
            "clip_ids": clip_ids,
            "mode": mode,
            "session_id": session_id,
        },
    )


async def _process(command: list[str], job_id: int, stop: asyncio.Event, log_path: Path) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("wb") as log:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=log,
            stderr=log,
            creationflags=flags,
        )
        try:
            while proc.returncode is None:
                if stop.is_set() or queue_manager.is_cancelled(job_id):
                    raise RuntimeError("Previs cancelled or worker stopped")
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except TimeoutError:
                    pass
            if proc.returncode:
                raise RuntimeError(
                    f"Previs process failed ({proc.returncode}); see {log_path.name}"
                )
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def wait_resources(job_id: int, stop: asyncio.Event, evidence: Path):
    client = ComfyUIClient()
    idle_since = None
    started = time.monotonic()
    try:
        while time.monotonic() - started < 3600:
            if stop.is_set() or queue_manager.is_cancelled(job_id):
                raise RuntimeError("Previs cancelled before rendering")
            observation = {"known": False}
            try:
                queue = await client.get_queue()
                proc = await asyncio.create_subprocess_exec(
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    output, _ = await asyncio.wait_for(proc.communicate(), 10)
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
                gpus = [
                    list(map(int, line.split(","))) for line in output.decode().strip().splitlines()
                ]
                observation = {
                    "render_device": "CPU",
                    "threads": 4,
                    "known": proc.returncode == 0 and bool(gpus),
                    "queue": queue,
                    "gpus": gpus,
                }
                idle = (
                    observation["known"]
                    and not queue.get("queue_running")
                    and not queue.get("queue_pending")
                )
            except Exception as exc:
                observation["error"] = str(exc)
                idle = False
            now = time.monotonic()
            idle_since = (idle_since if idle_since is not None else now) if idle else None
            observation["idle_seconds"] = 0 if idle_since is None else now - idle_since
            atomic_write(evidence, json.dumps(observation, indent=2))
            if idle_since is not None and now - idle_since >= 5:
                return
            await event_bus.publish(
                "job.progress",
                {
                    "job_id": job_id,
                    "message": "Waiting for the shared Comfy queue before CPU previs",
                },
            )
            await asyncio.sleep(5)
        raise RuntimeError("Shared rendering resources did not become idle")
    finally:
        await client.close()


async def run_previs(job: dict, payload: dict, stop: asyncio.Event) -> list[str]:
    for board in payload["document"].get("boards", []):
        if hashlib.sha256(Path(board["path"]).read_bytes()).hexdigest() != board["sha256"]:
            raise ValueError("Rough board changed after previs was queued")
    document = ProductionDocument.model_validate(payload["document"]).model_dump(mode="json")
    if (
        document["project_id"] != job["project_id"]
        or source_hash(payload["document"]) != payload["source_hash"]
    ):
        raise ValueError("Previs snapshot does not match this job")
    root = (
        settings.assets_dir / str(job["project_id"]) / "previs" / str(job["id"]) / str(uuid.uuid4())
    )
    root.mkdir(parents=True, exist_ok=True)
    request = root / "request.json"
    atomic_write(request, json.dumps(payload, indent=2))
    await wait_resources(job["id"], stop, root / "resources.json")
    await event_bus.publish(
        "job.progress",
        {
            "job_id": job["id"],
            "project_id": job["project_id"],
            "message": "Building shared Blender world and camera views",
        },
    )
    await _process(
        [
            blender_binary(),
            "--background",
            "--factory-startup",
            "--threads",
            "4",
            "--python-exit-code",
            "1",
            "--python",
            str(Path(__file__).with_name("blender_scene.py")),
            "--",
            str(request),
        ],
        job["id"],
        stop,
        root / "blender.log",
    )
    rendered = json.loads((root / "rendered.json").read_text(encoding="utf-8"))
    paths = [str(root / "world.blend")]
    paths.extend(str(root / item["file"]) for item in rendered["artifacts"])
    if payload["mode"] == "animation":
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to encode moving previs")
        for shot in document["shots"]:
            if shot["clip_id"] not in payload["clip_ids"]:
                continue
            target = root / f"shot-{shot['clip_id']}.mp4"
            await _process(
                [
                    ffmpeg,
                    "-nostdin",
                    "-y",
                    "-framerate",
                    "24",
                    "-start_number",
                    str(shot["frame_start"]),
                    "-i",
                    str(root / f"shot-{shot['clip_id']}-%06d.png"),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "18",
                    str(target),
                ],
                job["id"],
                stop,
                root / f"encode-{shot['clip_id']}.log",
            )
            paths.append(str(target))
    atomic_write(
        root / "receipt.json",
        json.dumps(
            {
                "job_id": job["id"],
                "source_hash": payload["source_hash"],
                "shot_hashes": payload["shot_hashes"],
                "outputs": paths,
                **rendered,
            },
            indent=2,
        ),
    )
    return paths

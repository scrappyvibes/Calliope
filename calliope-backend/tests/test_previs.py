import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from calliope.config import settings
from calliope.db import get_db
from calliope.previs import enqueue_previs, run_previs
from calliope.production import Camera, ProductionStore
from calliope.queue.manager import queue_manager


@pytest.fixture
def shot(client, monkeypatch):
    monkeypatch.setattr(queue_manager, "paused", True)
    monkeypatch.setattr("calliope.previs.blender_binary", lambda: "blender")
    pid = client.post("/api/projects", json={"title": "Previs test"}).json()["id"]
    db = get_db(settings.db_path)
    sid = db.execute("INSERT INTO scenes(project_id,order_index) VALUES (?,1)", (pid,)).lastrowid
    cid = db.execute(
        "INSERT INTO clips(project_id,scene_id,order_index) VALUES (?,?,1)", (pid, sid)
    ).lastrowid
    db.commit()
    db.close()
    ProductionStore(pid).set_camera(cid, Camera(), expected_revision=0)
    return pid, cid


def test_previs_pins_snapshot_before_later_camera_edit(shot):
    pid, cid = shot
    job = enqueue_previs(pid, 1, [cid])
    payload = json.loads(job["payload_json"])
    ProductionStore(pid).set_camera(cid, Camera(lens_mm=75), expected_revision=1)
    assert payload["document"]["shots"][0]["camera"]["lens_mm"] == 40
    with pytest.raises(ValueError, match="changed"):
        enqueue_previs(pid, 1, [cid])
    with pytest.raises(ValueError, match="saved camera"):
        enqueue_previs(pid, 2, [cid + 99])


def test_rough_board_is_bound_and_pinned_in_blender_document(shot):
    from calliope.storyboards import register_image
    from tests.test_storyboards import png

    pid, cid = shot
    state = register_image(pid, expected_revision=1, clip_id=cid, stage="rough", data=png())
    board = state["boards"][0]
    store = ProductionStore(pid)
    store.set_camera(cid, Camera(), expected_revision=2, rough_board_id=board["id"])
    job = enqueue_previs(pid, 3, [cid])
    payload = json.loads(job["payload_json"])
    assert payload["document"]["shots"][0]["rough_board_id"] == board["id"]
    assert payload["document"]["boards"][0]["sha256"] == board["sha256"]
    from pathlib import Path
    Path(board["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Rough board changed"):
        enqueue_previs(pid, 3, [cid])


def test_camera_rejects_other_shot_board_and_preserves_existing_hash(shot):
    from calliope.production import source_hash

    pid, cid = shot
    store = ProductionStore(pid)
    state = store.read()
    camera_data = {k: v for k, v in state["shots"][0].items() if k not in ("source_hash", "rough_board_id")}
    assert state["shots"][0]["source_hash"] == source_hash({"world":state["world"],"shot":camera_data})
    with pytest.raises(ValueError, match="rough board from this shot"):
        store.set_camera(cid, Camera(), expected_revision=1, rough_board_id="unknown")


def test_previs_route_and_mode_validation(shot, client):
    pid, cid = shot
    response = client.post(
        f"/api/projects/{pid}/production/previs",
        json={
            "expected_revision": 1,
            "clip_ids": [cid],
            "mode": "stills",
        },
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "previs"
    with pytest.raises(ValueError, match="mode"):
        enqueue_previs(pid, 1, [cid], "python")


@pytest.mark.asyncio
async def test_tampered_snapshot_never_launches_blender(shot, monkeypatch):
    pid, cid = shot
    job = enqueue_previs(pid, 1, [cid])
    payload = json.loads(job["payload_json"])
    payload["document"]["project_id"] += 1
    process = AsyncMock()
    monkeypatch.setattr("calliope.previs._process", process)
    with pytest.raises(ValueError, match="snapshot"):
        await run_previs(job, payload, asyncio.Event())
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_previs_cancellation_reaps_owned_process(shot, tmp_path, monkeypatch):
    import sys

    from calliope.previs import _process

    pid, cid = shot
    job = enqueue_previs(pid, 1, [cid])
    created = []
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        child = await original(*args, **kwargs)
        created.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        await _process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            job["id"],
            stop,
            tmp_path / "child.log",
        )
    assert len(created) == 1
    assert created[0].returncode is not None


@pytest.mark.asyncio
async def test_cpu_previs_records_busy_gpu_without_claiming_it(shot, monkeypatch, tmp_path):
    from unittest.mock import Mock

    from calliope.comfyui.client import ComfyUIClient
    from calliope.previs import wait_resources

    pid, cid = shot
    job = enqueue_previs(pid, 1, [cid])
    monkeypatch.setattr(
        ComfyUIClient,
        "get_queue",
        AsyncMock(
            return_value={
                "queue_running": [],
                "queue_pending": [],
            }
        ),
    )
    process = Mock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"70, 2500, 24000\n", None))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    evidence = tmp_path / "resources.json"
    await asyncio.wait_for(wait_resources(job["id"], asyncio.Event(), evidence), timeout=15)
    data = json.loads(evidence.read_text())
    assert data["render_device"] == "CPU"
    assert data["gpus"][0][0] == 70
    assert data["idle_seconds"] >= 5

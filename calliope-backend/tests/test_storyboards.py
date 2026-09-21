import io
import json

import pytest
from PIL import Image

from calliope.config import settings
from calliope.db import get_db
from calliope.production import Camera, ProductionStore
from calliope.queue.manager import queue_manager
from calliope.storyboards import enqueue_board, register_image


def png():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(data, format="PNG")
    return data.getvalue()


@pytest.fixture
def board_project(client, monkeypatch):
    monkeypatch.setattr(queue_manager, "paused", True)
    pid = client.post("/api/projects", json={"title": "Board chain"}).json()["id"]
    conn = get_db(settings.db_path)
    sid = conn.execute("INSERT INTO scenes(project_id,order_index) VALUES (?,1)", (pid,)).lastrowid
    cid = conn.execute(
        "INSERT INTO clips(project_id,scene_id,order_index) VALUES (?,?,1)", (pid, sid)
    ).lastrowid
    conn.commit()
    conn.close()
    store = ProductionStore(pid)
    state = store.set_camera(cid, Camera(), expected_revision=0)
    frame = settings.assets_dir / str(pid) / "previs" / f"shot-{cid}-000001.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(png())
    job = queue_manager.enqueue(
        project_id=pid,
        kind="previs",
        payload={
            "shot_hashes": {str(cid): state["shots"][0]["source_hash"]},
        },
    )
    queue_manager.mark_done(job["id"], [str(frame)])
    return pid, cid, job["id"]


def test_refined_and_styled_boards_keep_lineage_and_old_takes(board_project):
    pid, cid, jid = board_project
    state = register_image(
        pid,
        expected_revision=1,
        clip_id=cid,
        stage="refined",
        data=png(),
        previs_job_id=jid,
        previs_frame=1,
    )
    refined = state["boards"][0]
    assert not state["selected_boards"]
    state = register_image(
        pid,
        expected_revision=2,
        clip_id=cid,
        stage="styled",
        source_board_id=refined["id"],
        data=png(),
    )
    styled = state["boards"][1]
    assert styled["previs_job_id"] == jid
    assert styled["source_board_id"] == refined["id"]
    store = ProductionStore(pid)
    state = store.select_board(styled["id"], expected_revision=3)
    assert state["selected_boards"][f"{cid}:styled"] == styled["id"]
    state = store.set_camera(cid, Camera(lens_mm=80), expected_revision=4)
    assert len(state["boards"]) == 2
    assert all(b["stale"] for b in state["boards"])
    with pytest.raises(ValueError, match="earlier camera"):
        store.select_board(styled["id"], expected_revision=5)


def test_refined_board_requires_real_frame_and_styled_requires_refined(board_project):
    pid, cid, jid = board_project
    with pytest.raises(ValueError, match="Blender job and frame"):
        register_image(pid, expected_revision=1, clip_id=cid, stage="refined", data=png())
    with pytest.raises(ValueError, match="not rendered"):
        register_image(
            pid,
            expected_revision=1,
            clip_id=cid,
            stage="refined",
            data=png(),
            previs_job_id=jid,
            previs_frame=99,
        )
    state = register_image(pid, expected_revision=1, clip_id=cid, stage="rough", data=png())
    with pytest.raises(ValueError, match="refined source"):
        register_image(
            pid,
            expected_revision=2,
            clip_id=cid,
            stage="styled",
            data=png(),
            source_board_id=state["boards"][0]["id"],
        )


def test_upload_rejects_invalid_image_and_revision_conflict(board_project, client):
    pid, cid, jid = board_project
    metadata = {"expected_revision": 1, "clip_id": cid, "stage": "rough"}
    response = client.post(
        f"/api/projects/{pid}/production/boards/upload",
        data={"metadata": json.dumps(metadata)},
        files={"file": ("board.png", b"not image", "image/png")},
    )
    assert response.status_code == 400
    response = client.post(
        f"/api/projects/{pid}/production/boards/upload",
        data={"metadata": json.dumps(metadata)},
        files={"file": ("board.png", png(), "image/png")},
    )
    assert response.status_code == 200
    assert len(response.json()["boards"]) == 1
    response = client.post(
        f"/api/projects/{pid}/production/boards/upload",
        data={"metadata": json.dumps(metadata)},
        files={"file": ("board.png", png(), "image/png")},
    )
    assert response.status_code == 409


def test_generation_pins_graph_and_binds_recorded_camera_frame(board_project, client):
    pid, cid, jid = board_project
    graph = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": ""},
            "_meta": {"title": "Board (Input:image)"},
        },
        "2": {
            "class_type": "PrimitiveStringMultiline",
            "inputs": {"value": "original"},
            "_meta": {"title": "Prompt (Input:prompt)"},
        },
    }
    workflow = client.post(
        "/api/workflows", json={"name": "Refine test", "kind": "image", "workflow_json": graph}
    ).json()
    job = enqueue_board(
        pid,
        expected_revision=1,
        clip_id=cid,
        stage="refined",
        workflow_id=workflow["id"],
        input_values={"2": "preserve camera"},
        previs_job_id=jid,
        previs_frame=1,
    )
    payload = json.loads(job["payload_json"])
    assert payload["workflow_snapshot"]["2"]["inputs"]["value"] == "preserve camera"
    assert payload["workflow_snapshot"]["1"]["inputs"]["image"].endswith(f"shot-{cid}-000001.png")
    assert payload["storyboard_target"]["previs_job_id"] == jid
    assert payload["reference_manifest"][0]["node_id"] == "1"
    assert len(payload["reference_manifest"][0]["sha256"]) == 64
    with pytest.raises(ValueError, match="Unknown workflow input"):
        enqueue_board(
            pid,
            expected_revision=1,
            clip_id=cid,
            stage="refined",
            workflow_id=workflow["id"],
            input_values={"999": "wrong"},
            previs_job_id=jid,
            previs_frame=1,
        )


@pytest.mark.asyncio
async def test_same_named_images_get_distinct_content_addresses(tmp_path):
    import httpx

    from calliope.comfyui.client import ComfyUIClient

    first = tmp_path / "first" / "frame.png"
    second = tmp_path / "second" / "frame.png"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first image")
    second.write_bytes(b"second image")
    comfy = ComfyUIClient("http://comfy.test")
    await comfy._http.aclose()
    comfy._http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    try:
        one = await comfy.upload_image(first)
        two = await comfy.upload_image(second)
        assert one != two
        assert one == await comfy.upload_image(first)
        assert one.endswith(".png") and two.endswith(".png")
    finally:
        await comfy.close()


@pytest.mark.asyncio
async def test_changed_reference_stops_before_submission(board_project, monkeypatch):
    import hashlib
    from unittest.mock import AsyncMock

    from calliope.comfyui.client import ComfyUIClient
    from calliope.queue.worker import queue_worker

    pid, cid, _ = board_project
    path = settings.assets_dir / str(pid) / "previs" / f"shot-{cid}-000001.png"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    job = queue_manager.enqueue(
        project_id=pid,
        clip_id=cid,
        kind="image",
        payload={
            "reference_manifest": [{"path": str(path), "sha256": digest}],
        },
    )
    path.write_bytes(b"changed after enqueue")
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(ComfyUIClient, "health", AsyncMock(return_value=True))
    submit = AsyncMock()
    monkeypatch.setattr(ComfyUIClient, "queue_prompt", submit)
    with pytest.raises(RuntimeError, match="Reference changed"):
        await queue_worker._run_job(job)
    submit.assert_not_awaited()

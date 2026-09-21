import json
from pathlib import Path

import pytest

from calliope.comfyui.adapters import adapt_h3_workflow
from calliope.config import settings
from calliope.production import Camera, ProductionStore
from calliope.queue.manager import queue_manager
from calliope.storyboards import register_image
from calliope.video_production import enqueue_video
from tests.test_storyboards import board_project, png  # Shared real production/queue fixture.


@pytest.fixture
def video_request(board_project, client, monkeypatch):
    pid, cid, jid = board_project
    state = register_image(pid, expected_revision=1, clip_id=cid, stage="refined",
                           data=png(), previs_job_id=jid, previs_frame=1)
    state = register_image(pid, expected_revision=2, clip_id=cid, stage="styled",
                           data=png(), source_board_id=state["boards"][0]["id"])
    video = settings.assets_dir / str(pid) / "previs" / f"shot-{cid}.mp4"
    video.write_bytes(b"mock-video")
    job = queue_manager.enqueue(project_id=pid, kind="previs", payload={
        "shot_hashes": {str(cid): state["shots"][0]["source_hash"]},
    })
    queue_manager.mark_done(job["id"], [str(video)])
    monkeypatch.setattr("calliope.video_production._media", lambda path, kind: {"duration_seconds": 2} if kind == "video" else {})
    source = json.loads((Path(__file__).parent / "fixtures/h3_resolved.json").read_text())
    graph = adapt_h3_workflow(source, video_count=1)["workflow_json"]
    workflow = client.post("/api/workflows", json={"name": "H3 test", "kind": "video", "workflow_json": graph}).json()
    return pid, dict(expected_revision=3, clip_id=cid, workflow_id=workflow["id"],
                     styled_board_id=state["boards"][1]["id"], previs_job_id=job["id"],
                     prompt="Appearance from <Picture 1>; camera from <Video 1>.",
                     duration_seconds=5, seed=123)


def test_video_freezes_exact_graph_sources_and_grid(video_request):
    pid, request = video_request
    job = enqueue_video(pid, **request)
    payload = json.loads(job["payload_json"])
    assert payload["workflow_snapshot"]["334"]["inputs"]["text"] == request["prompt"]
    assert payload["workflow_snapshot"]["259"]["inputs"]["value"] == 5
    assert payload["production_video_target"]["frame_count"] == 124
    assert payload["production_video_target"]["actual_duration_seconds"] == 124 / 24
    assert payload["production_video_target"]["styled_board_id"] == request["styled_board_id"]
    assert {r["label"] for r in payload["reference_manifest"]} == {"Picture 1", "Video 1"}
    assert all(len(r["sha256"]) == 64 for r in payload["reference_manifest"])
    assert payload["input_values"] == {}


@pytest.mark.parametrize("change,match", [
    ({"prompt": "Use <Picture 2> and <Video 1>"}, "not bound"),
    ({"prompt": "Some scene"}, "Describe the roles"),
    ({"duration_seconds": 4}, "5–15"),
    ({"seed": -1}, "unsigned"),
    ({"reference_paths": {"51": "foreign.png"}}, "cannot be overridden"),
    ({"previs_job_id": None}, "completed Blender"),
])
def test_invalid_inputs_do_not_enqueue(video_request, change, match):
    pid, request = video_request
    before = len(queue_manager.list_jobs(project_id=pid))
    with pytest.raises(ValueError, match=match):
        enqueue_video(pid, **(request | change))
    assert len(queue_manager.list_jobs(project_id=pid)) == before


def test_camera_change_rejects_old_board(video_request):
    pid, request = video_request
    ProductionStore(pid).set_camera(request["clip_id"], Camera(lens_mm=85), expected_revision=3)
    with pytest.raises(ValueError, match="earlier camera"):
        enqueue_video(pid, **(request | {"expected_revision": 4}))


def test_changed_styled_file_is_rejected(video_request):
    pid, request = video_request
    board = ProductionStore(pid).read()["boards"][1]
    Path(board["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="image changed"):
        enqueue_video(pid, **request)


def test_candidate_does_not_replace_selected_clip(video_request):
    from calliope.db import get_db
    from calliope.queue.worker import queue_worker

    pid, request = video_request
    job = enqueue_video(pid, **request)
    queue_worker._apply_outputs_to_entities(job, json.loads(job["payload_json"]), ["new-candidate.mp4"])
    conn = get_db(settings.db_path)
    try:
        assert conn.execute("SELECT clip_path FROM clips WHERE id=?", (request["clip_id"],)).fetchone()[0] is None
    finally:
        conn.close()


def test_video_generation_endpoint(video_request, client):
    pid, request = video_request
    response = client.post(f"/api/projects/{pid}/production/videos/generate", json=request)
    assert response.status_code == 200
    assert response.json()["kind"] == "video"


def test_selected_take_survives_reload_and_repairs_clip_projection(video_request):
    from calliope.db import get_db

    pid, request = video_request
    job = enqueue_video(pid, **request)
    path = settings.assets_dir / str(pid) / "candidate.mp4"
    path.write_bytes(b"candidate")
    queue_manager.mark_done(job["id"], [str(path)])
    store = ProductionStore(pid)
    state = store.select_video_output(job["id"], 0, expected_revision=3)
    assert state["selected_videos"][str(request["clip_id"])] == f'{job["id"]}:0'
    assert not state["video_takes"][0]["stale"]
    conn = get_db(settings.db_path)
    try:
        assert conn.execute("SELECT clip_path FROM clips WHERE id=?", (request["clip_id"],)).fetchone()[0] == str(path.resolve())
        conn.execute("UPDATE clips SET clip_path=NULL WHERE id=?", (request["clip_id"],))
        conn.commit()
        assert ProductionStore(pid).read()["selected_videos"] == state["selected_videos"]
        assert conn.execute("SELECT clip_path FROM clips WHERE id=?", (request["clip_id"],)).fetchone()[0] == str(path.resolve())
    finally:
        conn.close()
    state = store.set_camera(request["clip_id"], Camera(lens_mm=80), expected_revision=4)
    assert state["video_takes"][0]["stale"]
    with pytest.raises(ValueError, match="earlier camera"):
        store.select_video_output(job["id"], 0, expected_revision=5)


def test_selection_rejects_changed_previously_selected_video(video_request):
    pid, request = video_request
    job = enqueue_video(pid, **request)
    path = settings.assets_dir / str(pid) / "candidate.mp4"
    path.write_bytes(b"candidate")
    queue_manager.mark_done(job["id"], [str(path)])
    store = ProductionStore(pid)
    store.select_video_output(job["id"], 0, expected_revision=3)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="file has changed"):
        store.select_video_output(job["id"], 0, expected_revision=4)


def test_media_probe_rejects_bad_stream_and_timeout(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from calliope.video_production import _media

    monkeypatch.setattr("calliope.video_production.shutil.which", lambda _: "ffprobe")
    monkeypatch.setattr("calliope.video_production.subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"5"}}'))
    with pytest.raises(ValueError, match="video stream"):
        _media(tmp_path / "fake.mp4", "video")
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("ffprobe", 30)
    monkeypatch.setattr("calliope.video_production.subprocess.run", timeout)
    with pytest.raises(ValueError, match="timed out"):
        _media(tmp_path / "fake.mp4", "video")

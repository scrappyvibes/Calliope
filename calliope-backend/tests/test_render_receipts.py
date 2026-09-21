import json
from unittest.mock import AsyncMock

import httpx
import pytest

from calliope.comfyui.client import ComfyUIClient
from calliope.queue.manager import queue_manager
from calliope.queue.receipts import ensure_submitted, latest_receipt


@pytest.fixture
def render_job(client, monkeypatch):
    monkeypatch.setattr(queue_manager, "paused", True)
    pid = client.post("/api/projects", json={"title": "Receipt proof"}).json()["id"]
    return queue_manager.enqueue(project_id=pid, kind="image")


@pytest.mark.asyncio
async def test_lost_submit_response_reconciles_without_duplicate(render_job):
    remote = {}
    submissions = []

    async def handler(request):
        if request.url.path == "/prompt":
            payload = json.loads(request.content)
            submissions.append(payload)
            remote.update(payload)
            raise httpx.ReadTimeout("response lost after acceptance")
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={
                    "queue_running": [
                        [0, remote["prompt_id"], remote["prompt"], remote["extra_data"], []]
                    ],
                    "queue_pending": [],
                },
            )
        raise AssertionError(request.url)

    comfy = ComfyUIClient("http://comfy.test")
    await comfy._http.aclose()
    comfy._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prepare = AsyncMock(return_value={"1": {"class_type": "Test", "inputs": {}}})
    try:
        with pytest.raises(RuntimeError, match="not resubmitted"):
            await ensure_submitted(render_job["id"], comfy, prepare)
        receipt = latest_receipt(render_job["id"])
        assert receipt["state"] == "unknown"
        recovered, history = await ensure_submitted(render_job["id"], comfy, prepare)
        assert recovered["prompt_id"] == remote["prompt_id"]
        assert history is None
        assert len(submissions) == 1
        assert prepare.await_count == 1
    finally:
        await comfy.close()


@pytest.mark.asyncio
async def test_restart_recovers_server_assigned_id_from_history(render_job):
    calls = []
    remote = {}

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/prompt":
            remote.update(json.loads(request.content))
            raise httpx.ReadTimeout("legacy server minted a different ID")
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        if request.url.path == "/history":
            return httpx.Response(
                200,
                json={
                    "server-generated-id": {
                        "prompt": [
                            0,
                            "server-generated-id",
                            remote["prompt"],
                            remote["extra_data"],
                            [],
                        ],
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {},
                    }
                },
            )
        raise AssertionError(request.url)

    comfy = ComfyUIClient("http://comfy.test")
    await comfy._http.aclose()
    comfy._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prepare = AsyncMock(return_value={"1": {"class_type": "Test", "inputs": {}}})
    try:
        with pytest.raises(RuntimeError):
            await ensure_submitted(render_job["id"], comfy, prepare)
        receipt, history = await ensure_submitted(render_job["id"], comfy, prepare)
        assert receipt["prompt_id"] == "server-generated-id"
        assert history["status"]["completed"]
        assert calls.count("/prompt") == 1
    finally:
        await comfy.close()


@pytest.mark.asyncio
async def test_cancel_only_deletes_named_pending_prompt():
    calls = []

    async def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    comfy = ComfyUIClient("http://comfy.test")
    await comfy._http.aclose()
    comfy._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await comfy.delete_pending_prompt("owned-prompt")
        assert calls == [("/queue", {"delete": ["owned-prompt"]})]
    finally:
        await comfy.close()


def test_cancelled_job_cannot_be_overwritten_as_done(render_job):
    job_id = render_job["id"]
    queue_manager.cancel(job_id)
    queue_manager.mark_done(job_id, ["late-output.png"])
    queue_manager.mark_failed(job_id, "late error")
    job = queue_manager.get_job(job_id)
    assert job["status"] == "failed"
    assert job["error"] == "cancelled"


@pytest.mark.asyncio
async def test_missing_history_does_not_authorize_resubmission(render_job):
    comfy = ComfyUIClient("http://comfy.test")
    comfy.queue_prompt = AsyncMock(return_value="accepted-id")
    comfy.get_history = AsyncMock(return_value=None)
    comfy.get_queue = AsyncMock(return_value={})
    comfy.recent_history = AsyncMock(return_value={})
    prepare = AsyncMock(return_value={})
    try:
        await ensure_submitted(render_job["id"], comfy, prepare)
        with pytest.raises(RuntimeError, match="not resubmitted"):
            await ensure_submitted(render_job["id"], comfy, prepare)
        assert comfy.queue_prompt.await_count == 1
        assert prepare.await_count == 1
    finally:
        await comfy.close()


@pytest.mark.asyncio
async def test_cancel_uses_receipt_server_and_prompt(render_job, client, monkeypatch):
    comfy = ComfyUIClient("http://original.test")
    comfy.queue_prompt = AsyncMock(return_value="owned-id")
    try:
        await ensure_submitted(render_job["id"], comfy, AsyncMock(return_value={}))
    finally:
        await comfy.close()
    deletions = []

    async def delete(self, prompt_id):
        deletions.append((self.base_url, prompt_id))

    interrupt = AsyncMock()
    monkeypatch.setattr(ComfyUIClient, "delete_pending_prompt", delete)
    monkeypatch.setattr(ComfyUIClient, "interrupt", interrupt)
    response = client.post(f"/api/jobs/{render_job['id']}/cancel")
    assert response.status_code == 200
    assert deletions == [("http://original.test", "owned-id")]
    assert "may finish" in response.json()["message"]
    interrupt.assert_not_awaited()


def test_legacy_running_job_requires_inspection(render_job, monkeypatch):
    from calliope import config

    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(queue_manager, "paused", False)
    claimed = queue_manager.claim_next()
    assert claimed["id"] == render_job["id"]
    queue_manager.reset_stale_jobs()
    job = queue_manager.get_job(claimed["id"])
    assert job["error"] == "untracked_comfy_render"
    assert queue_manager.retry(job["id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_definitive_rejection_allows_explicit_retry(render_job):
    from calliope.comfyui.client import WorkflowRejectedError

    comfy = ComfyUIClient("http://comfy.test")
    comfy.queue_prompt = AsyncMock(side_effect=[WorkflowRejectedError("invalid node"), "fixed-id"])
    prepare = AsyncMock(return_value={})
    try:
        with pytest.raises(WorkflowRejectedError):
            await ensure_submitted(render_job["id"], comfy, prepare)
        first = latest_receipt(render_job["id"])
        assert first["state"] == "rejected"
        second, _ = await ensure_submitted(render_job["id"], comfy, prepare)
        assert second["id"] != first["id"]
        assert second["prompt_id"] == "fixed-id"
        assert prepare.await_count == 2
    finally:
        await comfy.close()


@pytest.mark.asyncio
async def test_existing_render_cannot_move_to_a_different_server(render_job):
    original = ComfyUIClient("http://original.test")
    changed = ComfyUIClient("http://changed.test")
    original.queue_prompt = AsyncMock(return_value="accepted")
    changed.queue_prompt = AsyncMock()
    try:
        await ensure_submitted(render_job["id"], original, AsyncMock(return_value={}))
        with pytest.raises(RuntimeError, match="original ComfyUI server"):
            await ensure_submitted(render_job["id"], changed, AsyncMock())
        changed.queue_prompt.assert_not_awaited()
    finally:
        await original.close()
        await changed.close()

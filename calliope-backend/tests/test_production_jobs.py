import json

import pytest

from calliope.agent.harness.plugins.production import get_production_job
from calliope.agent.harness.registry import ToolContext
from calliope.queue.manager import queue_manager


@pytest.mark.asyncio
async def test_job_read_retains_lineage_without_graph_and_paginates(monkeypatch):
    lineage = {"stage": "styled", "source_board_id": "refined", "previs_job_id": 8}
    job = {
        "id": 12,
        "project_id": 3,
        "workflow_id": 1,
        "kind": "image",
        "status": "done",
        "error": None,
        "payload_json": json.dumps(
            {"workflow_snapshot": {"large": "x" * 20000}, "storyboard_target": lineage}
        ),
        "output_paths_json": json.dumps([f"frame-{i}.png" for i in range(240)]),
    }
    monkeypatch.setattr(queue_manager, "get_job", lambda job_id: job)
    result = await get_production_job(
        ToolContext(session_id=1, project_id=3),
        {"job_id": 12, "output_offset": 120, "output_limit": 2},
    )
    assert result["storyboard_target"] == lineage
    assert result["outputs"] == [
        {"output_index": 120, "path": "frame-120.png"},
        {"output_index": 121, "path": "frame-121.png"},
    ]
    assert result["next_output_offset"] == 122
    assert result["output_count"] == 240
    assert len(json.dumps(result)) < 1000
    with pytest.raises(ValueError, match="does not belong"):
        await get_production_job(ToolContext(session_id=1, project_id=4), {"job_id": 12})

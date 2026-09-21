import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from calliope.agent.harness import orchestrator as orch
from calliope.agent.harness.registry import ToolContext
from calliope.production import ProductionStore


@pytest.mark.asyncio
async def test_failure_preserves_actual_saved_progress_without_synthesis(client, monkeypatch):
    pid = client.post("/api/projects", json={"title": "Recovery"}).json()["id"]
    sid = client.post("/api/agent/sessions", json={"title": "Recovery"}).json()["id"]
    llm = SimpleNamespace(
        chat_with_tools=AsyncMock(
            side_effect=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "save",
                            "type": "function",
                            "function": {
                                "name": "set_production_world",
                                "arguments": json.dumps(
                                    {"expected_revision": 0, "world": {"objects": []}}
                                ),
                            },
                        }
                    ],
                },
                RuntimeError("subscription unavailable"),
            ]
        ),
        chat=AsyncMock(side_effect=AssertionError("Failure must not be rewritten by a model")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(orch, "_llm_for_role", lambda role: llm)
    monkeypatch.setattr(
        orch,
        "_plan",
        AsyncMock(
            return_value={
                "mode": "swarm",
                "tasks": [
                    {"role": "production", "goal": "Save an empty blockout"},
                    {"role": "production", "goal": "Do not start this dependent task"},
                ],
            }
        ),
    )
    result = await orch.orchestrate(
        ToolContext(session_id=sid, project_id=pid),
        [{"role": "user", "content": "Build a scene"}],
        session_id=sid,
    )
    assert ProductionStore(pid).read()["revision"] == 1
    assert "Work interrupted" in result
    assert '"tool": "set_production_world"' in result
    assert '"production_revision": 1' in result
    assert "not rolled back" in result
    assert llm.chat_with_tools.await_count == 2
    llm.chat.assert_not_awaited()

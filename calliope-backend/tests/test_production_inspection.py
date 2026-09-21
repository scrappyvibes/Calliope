from unittest.mock import AsyncMock

import pytest

from calliope.agent.harness.plugins.production import inspect_image
from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.storyboards import register_image
from tests.test_storyboards import board_project as board_project
from tests.test_storyboards import png


@pytest.mark.asyncio
async def test_inspection_sends_actual_owned_image_to_selected_subscription(
    board_project, monkeypatch
):
    pid, cid, _ = board_project
    state = register_image(pid, expected_revision=1, clip_id=cid, stage="rough", data=png())
    board = state["boards"][0]
    monkeypatch.setattr(settings, "completion_provider", "claude")
    chat = AsyncMock(return_value="Blue square; no visible geometry.")
    monkeypatch.setattr("calliope.agent.llm.LLMClient.chat", chat)
    result = await inspect_image(
        ToolContext(session_id=1, project_id=pid), {"board_id": board["id"]}
    )
    assert result["sha256"] == board["sha256"]
    assert result["provider"] == "claude"
    assert chat.call_args.args[0][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.asyncio
async def test_inspection_rejects_unknown_board_without_calling_model(board_project, monkeypatch):
    pid, _, _ = board_project
    monkeypatch.setattr(settings, "completion_provider", "codex")
    chat = AsyncMock()
    monkeypatch.setattr("calliope.agent.llm.LLMClient.chat", chat)
    with pytest.raises(ValueError, match="does not belong"):
        await inspect_image(ToolContext(session_id=1, project_id=pid), {"board_id": "foreign"})
    chat.assert_not_called()


@pytest.mark.asyncio
async def test_comparison_supplies_both_images_and_provenance(board_project, monkeypatch):
    pid, cid, _ = board_project
    state = register_image(pid, expected_revision=1, clip_id=cid, stage="rough", data=png())
    board = state["boards"][0]
    monkeypatch.setattr(settings, "completion_provider", "claude")
    chat = AsyncMock(return_value="These two images match.")
    monkeypatch.setattr("calliope.agent.llm.LLMClient.chat", chat)
    result = await inspect_image(
        ToolContext(session_id=1, project_id=pid),
        {
            "board_id": board["id"],
            "compare_to": {"board_id": board["id"]},
        },
    )
    parts = chat.call_args.args[0][1]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 2
    assert images[0] == images[1]
    assert result["comparison"]["sha256"] == board["sha256"]


@pytest.mark.asyncio
async def test_comparison_rejects_unowned_reference_before_model(board_project, monkeypatch):
    pid, cid, _ = board_project
    state = register_image(pid, expected_revision=1, clip_id=cid, stage="rough", data=png())
    monkeypatch.setattr(settings, "completion_provider", "claude")
    chat = AsyncMock()
    monkeypatch.setattr("calliope.agent.llm.LLMClient.chat", chat)
    with pytest.raises(ValueError, match="does not belong"):
        await inspect_image(
            ToolContext(session_id=1, project_id=pid),
            {
                "board_id": state["boards"][0]["id"],
                "compare_to": {"board_id": "foreign"},
            },
        )
    chat.assert_not_called()

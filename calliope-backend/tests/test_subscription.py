import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from calliope.agent import subscription as sub
from calliope.agent.llm import LLMClient
from calliope.config import settings

TOOLS = [{"type": "function", "function": {"name": "add_beat", "parameters": {}}}]


def test_provider_setting_roundtrip_and_validation(client):
    assert client.post("/api/settings", json={"completion_provider": "claude"}).status_code == 200
    assert client.get("/api/settings").json()["completion_provider"] == "claude"
    assert client.post("/api/settings", json={"completion_provider": "unknown"}).status_code == 422
    assert client.post("/api/settings", json={"completion_provider": None}).status_code == 422


def test_environment_drops_api_billing_and_preserves_login(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("PATH", "cli-path")
    env = sub.subscription_env()
    assert not any(
        k in env
        for k in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
        )
    )
    assert env["PATH"] == "cli-path"


@pytest.mark.parametrize(
    "reply,choice",
    [
        ({"content": "", "tool_calls": [{"name": "delete_all", "arguments": "{}"}]}, None),
        ({"content": "", "tool_calls": [{"name": "add_beat", "arguments": "[]"}]}, None),
        ({"content": "", "tool_calls": [{"name": "add_beat", "arguments": "{}"}]}, "none"),
        ({"content": "hello", "tool_calls": []}, "required"),
        ({"content": "", "tool_calls": []}, None),
        ({"content": "hello", "tool_calls": []}, {"function": {"name": "add_beat"}}),
    ],
)
def test_invalid_actions_fail_closed(reply, choice):
    with pytest.raises(ValueError):
        sub.decode_reply(reply, TOOLS, choice)


def test_tool_reply_maps_to_existing_harness():
    reply = sub.decode_reply(
        {"content": "", "tool_calls": [{"name": "add_beat", "arguments": '{"title":"Arrival"}'}]},
        TOOLS,
        None,
    )
    assert reply["role"] == "assistant"
    assert reply["tool_calls"][0]["function"]["name"] == "add_beat"
    assert json.loads(reply["tool_calls"][0]["function"]["arguments"])["title"] == "Arrival"


def test_images_are_not_silently_discarded():
    with pytest.raises(ValueError, match="image attachments"):
        sub.completion_prompt([{"content": [{"type": "image_url", "image_url": {}}]}], [], None)


@pytest.mark.asyncio
async def test_auth_and_cli_flags(monkeypatch):
    monkeypatch.setattr(sub.shutil, "which", lambda name: "claude.exe")
    runner = AsyncMock(
        side_effect=[
            b'{"loggedIn":true,"authMethod":"claude.ai"}',
            b'{"structured_output":{"content":"hello","tool_calls":[]}}',
        ]
    )
    monkeypatch.setattr(sub, "_run_cli", runner)
    assert (await sub.claude_completion([]))["content"] == "hello"
    args = runner.call_args_list[1].args[0]
    assert args[args.index("--tools") + 1] == ""
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(args)
    assert "--bare" not in args
    system_prompt = args[args.index("--system-prompt") + 1]
    assert "host executes them after your response" in system_prompt
    assert "Do not simulate subsequent turns" in system_prompt


@pytest.mark.asyncio
async def test_api_login_is_rejected_before_generation(monkeypatch):
    monkeypatch.setattr(sub.shutil, "which", lambda name: "claude.exe")
    runner = AsyncMock(return_value=b'{"loggedIn":true,"authMethod":"apiKey"}')
    monkeypatch.setattr(sub, "_run_cli", runner)
    with pytest.raises(RuntimeError, match="Claude plan"):
        await sub.claude_completion([])
    assert runner.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_client_routes_all_completion_shapes_without_http(monkeypatch, provider):
    monkeypatch.setattr(settings, "completion_provider", provider)
    completion = AsyncMock(return_value={"content": "hello", "tool_calls": []})
    monkeypatch.setattr(sub, f"{provider}_completion", completion)
    client = LLMClient()
    monkeypatch.setattr(client.client, "post", AsyncMock(side_effect=AssertionError("API called")))
    try:
        assert await client.chat([]) == "hello"
        assert (await client.chat_with_tools([], tools=TOOLS))["content"] == "hello"
        assert completion.call_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_kills_only_owned_process(monkeypatch):
    process = AsyncMock()
    process.returncode = None
    killed = []
    process.kill = lambda: killed.append(True)

    async def hang(payload):
        await asyncio.sleep(30)

    process.communicate = hang
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(sub._run_cli(["claude"], b"prompt", timeout=60))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [True]
    process.wait.assert_awaited_once()


def image_message(url="data:image/png;base64,aW1hZ2U="):
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect the blocking"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]


def test_image_order_and_history_preserved():
    messages = image_message() + image_message()
    prepared, images = sub.prepare_images(messages)
    assert len(images) == 2
    assert prepared[0]["content"][-1]["text"] == "[Attached image 1]"
    assert prepared[1]["content"][-1]["text"] == "[Attached image 2]"
    assert messages[0]["content"][-1]["type"] == "image_url"
    assert base64.b64decode(images[0]["data"]) == b"image"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "file:///private.png",
        "data:image/png;base64,%%%",
        "data:image/svg+xml;base64,aW1hZ2U=",
        "data:image/png;base64,",
    ],
)
def test_invalid_or_remote_images_fail_before_cli(url):
    with pytest.raises(ValueError):
        sub.prepare_images(image_message(url))


@pytest.mark.asyncio
async def test_claude_image_stdin_is_real_multimodal(monkeypatch):
    monkeypatch.setattr(sub.shutil, "which", lambda name: "claude.exe")
    runner = AsyncMock(
        side_effect=[
            b'{"loggedIn":true,"authMethod":"claude.ai"}',
            b'{"type":"result","structured_output":{"content":"A board","tool_calls":[]}}\n',
        ]
    )
    monkeypatch.setattr(sub, "_run_cli", runner)
    assert (await sub.claude_completion(image_message()))["content"] == "A board"
    args, payload = runner.call_args_list[1].args
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json"
    parts = json.loads(payload)["message"]["content"]
    assert parts[-1]["source"]["data"] == "aW1hZ2U="
    assert parts[-1]["type"] == "image"


@pytest.mark.asyncio
async def test_codex_isolated_flags_and_image_bytes(monkeypatch):
    monkeypatch.setattr(sub.shutil, "which", lambda name: "codex.exe")
    calls = []

    async def run(args, payload, **kwargs):
        calls.append(args)
        if args[1:3] == ["login", "status"]:
            return b"Logged in using ChatGPT"
        assert {"--ignore-user-config", "--ignore-rules", "--ephemeral"} <= set(args)
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert args[args.index("--disable") + 1] == "shell_tool"
        assert 'forced_login_method="chatgpt"' in args
        assert Path(args[args.index("--image") + 1]).read_bytes() == b"image"
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text('{"content":"A board","tool_calls":[]}', encoding="utf-8")
        return b""

    monkeypatch.setattr(sub, "_run_cli", run)
    assert (await sub.codex_completion(image_message()))["content"] == "A board"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_codex_rejects_api_login(monkeypatch):
    monkeypatch.setattr(sub.shutil, "which", lambda name: "codex.exe")
    runner = AsyncMock(return_value=b"Logged in using an API key")
    monkeypatch.setattr(sub, "_run_cli", runner)
    with pytest.raises(RuntimeError, match="ChatGPT"):
        await sub.codex_completion([])
    assert runner.call_count == 1

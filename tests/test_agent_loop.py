"""M2 — agent loop, tools, system prompt.

Spec refs: DESIGN.md §3.5.1 (tools), §3.5.2 (A model: step() drains),
§3.6 ([radio] forced-insert format), §2.4 (FYI/URGENT prefixes).
"""

import json

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.server import MessageServer


class ScriptedBackend(ModelBackend):
    """Returns pre-scripted completions in order; records what it saw."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # (messages_snapshot, tool_names)

    async def complete(self, messages, tools):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tool_names": [t["name"] for t in tools]}
        )
        return self.script.pop(0)


def make_agent(server, agent_id, script):
    return AgentLoop(
        agent_id=agent_id,
        server=server,
        backend=ScriptedBackend(script),
        system_prompt="You are a radio agent.",
    )


# ---------------------------------------------------------------------------
# step(): inbox drain → single [radio] user turn (§3.6)
# ---------------------------------------------------------------------------


async def test_step_drains_inbox_into_single_radio_user_turn():
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="(URGENT) answer is 43", mentions=["agent-2"])

    agent = make_agent(server, "agent-2", [Completion(text="noted")])
    result = await agent.step()

    assert result.drained_count == 1
    # the model saw exactly one injected user turn containing the [radio] block
    backend = agent.backend
    user_msgs = [m for m in backend.calls[0]["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    content = user_msgs[0]["content"]
    assert "[radio]" in content
    assert "from agent-1" in content
    assert "(URGENT) answer is 43" in content
    # conversation now holds the turn too
    roles = [m["role"] for m in agent.conversation]
    assert roles.count("user") == 1


async def test_multiple_drained_messages_merge_into_one_turn():
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2", "agent-3"])
    await server.send_message(tid, author="agent-1", content="one", mentions=["agent-3"])
    await server.send_message(tid, author="agent-2", content="(FYI) two", mentions=["agent-3"])

    agent = make_agent(server, "agent-3", [Completion(text="ok")])
    result = await agent.step()

    assert result.drained_count == 2
    backend = agent.backend
    user_msgs = [m for m in backend.calls[0]["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert "from agent-1: one" in user_msgs[0]["content"]
    assert "from agent-2: (FYI) two" in user_msgs[0]["content"]


async def test_step_with_empty_inbox_still_runs_model():
    """No pending messages → plain step without a [radio] turn."""
    server = MessageServer()
    await server.create_thread("t", participants=["agent-1", "agent-2"])

    agent = make_agent(server, "agent-2", [Completion(text="working")])
    result = await agent.step()

    assert result.drained_count == 0
    assert result.text == "working"
    backend = agent.backend
    contents = " ".join(m.get("content") or "" for m in backend.calls[0]["messages"])
    assert "[radio]" not in contents


# ---------------------------------------------------------------------------
# tool execution through the loop
# ---------------------------------------------------------------------------


async def test_agent_calls_create_thread_then_send_message():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="create_thread", arguments={"name": "plan", "participants": ["agent-1", "agent-2"]}),
                ]
            ),
            Completion(
                tool_calls=[
                    ToolCall(id="c2", name="send_message", arguments={"thread": None, "content": "hi @agent-2", "mentions": ["agent-2"]}),
                ]
            ),
            Completion(text="done"),
        ],
    )
    await agent.step()
    await agent.step()

    # patch the scripted thread id into call 2 before executing:
    # simpler: verify thread exists and run third step with correct id
    snap = server.snapshot()
    assert len(snap["threads"]) == 1
    tid = snap["threads"][0]["thread_id"]

    # re-script the send step with the real thread id
    agent.backend.script.clear()
    agent.backend.script.append(
        Completion(
            tool_calls=[
                ToolCall(id="c3", name="send_message", arguments={"thread": tid, "content": "hi @agent-2", "mentions": ["agent-2"]}),
            ]
        )
    )
    await agent.step()
    assert server.inbox_size("agent-2") == 1
    # tool results recorded in conversation as role=tool
    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    assert len(tool_msgs) == 3
    assert all(m["content"].startswith("{") for m in tool_msgs)


async def test_read_resource_returns_snapshot_json():
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1"])
    await server.send_message(tid, author="agent-1", content="self note", mentions=[])

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(tool_calls=[ToolCall(id="c1", name="read_resource", arguments={})]),
            Completion(text="got it"),
        ],
    )
    await agent.step()
    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["threads"][0]["thread_id"] == tid
    assert len(payload["messages"]) == 1


# ---------------------------------------------------------------------------
# tool exposure by mode (§3.5.1 / §3.5.5)
# ---------------------------------------------------------------------------


def test_l3_exposes_six_tools():
    server = MessageServer()
    agent = make_agent(server, "agent-1", [])
    names = {t["name"] for t in agent.tool_specs}
    assert names == {
        "create_thread", "send_message", "read_resource",
        "read_file", "list_directory", "write_file",
    }


# ---------------------------------------------------------------------------
# system prompt carries the communication rules (§2.4)
# ---------------------------------------------------------------------------


async def test_system_prompt_contains_prefix_conventions_and_mention_syntax():
    from agent_augury.agent.system_prompt import render_system_prompt

    prompt = render_system_prompt("agent-2")
    assert "agent-2" in prompt
    assert "FYI:" in prompt and "URGENT:" in prompt
    assert "@agent-" in prompt  # mention surface syntax documented


# ---------------------------------------------------------------------------
# tool_call_id propagation (OpenAI multi-turn compatibility)
# ---------------------------------------------------------------------------


async def test_tool_call_id_propagated_to_tool_result_messages():
    """tool_call_id from the model request must appear in the tool result."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="call_abc123", name="create_thread", arguments={
                        "name": "plan", "participants": ["agent-1", "agent-2"]
                    }),
                ]
            ),
            Completion(text="done"),
        ],
    )
    await agent.step()

    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_abc123"


async def test_step_with_http_404_returns_error_text():
    """Backend returning HTTP 404 must surface error text, not raise."""
    from agent_augury.backend.openai_compat import OpenAICompatBackend
    import httpx

    def handler(request):
        return httpx.Response(404, json={"error": "Not Found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = OpenAICompatBackend(
        base_url="http://fake.local/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )
    server = MessageServer()
    server.register_agent("agent-1")
    agent = AgentLoop(
        agent_id="agent-1",
        server=server,
        backend=backend,
        system_prompt="test",
    )
    result = await agent.step()
    assert result.text is not None
    assert "[backend error]" in result.text
    assert "404" in result.text


# ---------------------------------------------------------------------------
# D9: allowed_roots security enforcement
# ---------------------------------------------------------------------------


async def test_toolbox_rejects_path_outside_allowed_roots(tmp_path):
    """ToolBox with allowed_roots rejects paths outside the root."""
    from agent_augury.agent.tools import ToolBox

    server = MessageServer()
    server.register_agent("agent-1")

    # Create allowed root + file outside it
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("secret", encoding="utf-8")

    tb = ToolBox(server, allowed_roots=[str(allowed_dir)])
    result = await tb.execute("agent-1", "read_file", {"path": str(secret_file)})
    payload = json.loads(result)
    assert "error" in payload
    assert "outside allowed roots" in payload["error"]


async def test_toolbox_allows_path_within_allowed_roots(tmp_path):
    """ToolBox with allowed_roots permits paths within the root."""
    from agent_augury.agent.tools import ToolBox

    server = MessageServer()
    server.register_agent("agent-1")

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    safe_file = allowed_dir / "safe.txt"
    safe_file.write_text("hello", encoding="utf-8")

    tb = ToolBox(server, allowed_roots=[str(allowed_dir)])
    result = await tb.execute("agent-1", "read_file", {"path": str(safe_file)})
    payload = json.loads(result)
    assert payload["content"] == "hello"


async def test_toolbox_none_allowed_roots_preserves_unrestricted(tmp_path):
    """allowed_roots=None preserves legacy unrestricted behavior (backward compat)."""
    from agent_augury.agent.tools import ToolBox

    server = MessageServer()
    server.register_agent("agent-1")

    any_file = tmp_path / "any.txt"
    any_file.write_text("free", encoding="utf-8")

    tb = ToolBox(server, allowed_roots=None)
    result = await tb.execute("agent-1", "read_file", {"path": str(any_file)})
    payload = json.loads(result)
    assert payload["content"] == "free"


async def test_toolbox_list_directory_respects_allowed_roots(tmp_path):
    """list_directory also respects allowed_roots."""
    from agent_augury.agent.tools import ToolBox

    server = MessageServer()
    server.register_agent("agent-1")

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    tb = ToolBox(server, allowed_roots=[str(allowed_dir)])
    result = await tb.execute("agent-1", "list_directory", {"path": str(outside_dir)})
    payload = json.loads(result)
    assert "error" in payload
    assert "outside allowed roots" in payload["error"]


async def test_toolbox_write_file_respects_allowed_roots(tmp_path):
    """write_file also respects allowed_roots."""
    from agent_augury.agent.tools import ToolBox

    server = MessageServer()
    server.register_agent("agent-1")

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_file = tmp_path / "outside.txt"

    tb = ToolBox(server, allowed_roots=[str(allowed_dir)])
    result = await tb.execute("agent-1", "write_file", {"path": str(outside_file), "content": "x"})
    payload = json.loads(result)
    assert "error" in payload
    assert "outside allowed roots" in payload["error"]

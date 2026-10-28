"""web_fetch, todo_write, skill, system prompt, and the task (subagent) tool."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kalash.agent.prompt import build_system_prompt
from kalash.llm.types import Usage
from kalash.skills import Skill
from kalash.tools.misc import SkillTool, WebFetchTool, check_public_host, html_to_text
from tests.harness.ctx import make_ctx, run_tool
from tests.harness.scripted import Reply, call, calls, say

# ----------------------------------------------------------------------------- web_fetch


async def resolve_as(table: dict[str, list[str]]):
    async def resolver(host: str, _port: int) -> list[str]:
        return table.get(host, ["93.184.216.34"])

    return resolver


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/x",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://user:pass@example.com/",
    ],
)
async def test_private_and_non_http_targets_are_blocked(url):
    async def local(host, _port):
        return {"localhost": ["127.0.0.1"]}.get(host, [host.strip("[]")])

    assert await check_public_host(url, local) is not None


async def test_public_address_is_allowed_and_dns_failure_is_reported():
    assert await check_public_host("https://example.com/a?b=1", await resolve_as({})) is None

    async def broken(_host, _port):
        raise OSError("name not known")

    assert "cannot resolve" in (await check_public_host("https://nope.test", broken) or "")


async def test_dns_rebinding_to_a_private_address_is_blocked():
    resolver = await resolve_as({"evil.test": ["10.1.2.3"]})
    assert "private" in (await check_public_host("http://evil.test/", resolver) or "")


async def fetch(work: Path, handler, url: str, resolver=None):
    ctx = make_ctx(
        work,
        fetch_transport=httpx.MockTransport(handler),
        fetch_resolver=resolver or await resolve_as({}),
    )
    return await run_tool(WebFetchTool(), ctx, url=url)


async def test_html_is_converted_to_readable_text(work: Path):
    page = (
        "<html><head><title>T</title><style>p{}</style></head><body><h1>Hello</h1>"
        "<script>evil()</script><p>First para.</p><p>Second &amp; last.</p></body></html>"
    )
    result = await fetch(
        work,
        lambda r: httpx.Response(
            200, text=page, headers={"content-type": "text/html; charset=utf-8"}
        ),
        "https://a.test/",
    )
    assert (
        "Hello" in result.text
        and "First para." in result.text
        and "Second & last." in (result.text)
    )
    assert "evil()" not in result.text and "<p>" not in result.text


async def test_redirect_to_a_private_host_is_refused(work: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.test":
            return httpx.Response(302, headers={"location": "http://internal.test/secret"})
        raise AssertionError("must not be requested")

    resolver = await resolve_as({"internal.test": ["10.0.0.9"]})
    result = await fetch(work, handler, "https://a.test/", resolver)
    assert result.is_error and "private or local address" in result.text


async def test_redirect_chain_is_bounded_and_status_and_type_errors_reported(work: Path):
    loop = await fetch(
        work, lambda r: httpx.Response(302, headers={"location": str(r.url)}), "https://a.test/"
    )
    assert loop.is_error and "Too many redirects" in loop.text
    missing = await fetch(work, lambda r: httpx.Response(404), "https://a.test/")
    assert missing.is_error and "HTTP 404" in missing.text
    binary = await fetch(
        work,
        lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}),
        "https://a.test/i.png",
    )
    assert binary.is_error and "not text" in binary.text


async def test_huge_body_is_capped_and_json_passes_through(work: Path):
    huge = await fetch(
        work,
        lambda r: httpx.Response(
            200, content=b"a" * 5_000_000, headers={"content-type": "text/plain"}
        ),
        "https://a.test/",
    )
    assert len(huge.text) < 40_000 and "saved to" in huge.text
    data = await fetch(work, lambda r: httpx.Response(200, json={"ok": True}), "https://a.test/j")
    assert '"ok":true' in data.text.replace(" ", "")


def test_html_to_text_collapses_whitespace():
    assert html_to_text("<div>  a \n\n b </div><div>c</div>") == "a b\n\nc"


# ----------------------------------------------------------------------------- todo / skill


async def test_todo_write_records_the_complete_list_in_the_session(make_harness):
    todos = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "pending"},
    ]
    h = await make_harness([call("todo_write", {"todos": todos}), say("planned")])
    await h.turn("plan it")
    assert h.session.todos() == todos
    result_text = [m.text for m in h.session.messages() if m.role == "tool"][0]
    assert "1 completed, 1 in progress, 1 pending" in result_text


async def test_todo_rejects_unknown_status(make_harness):
    bad = [{"content": "a", "status": "done"}]
    h = await make_harness([call("todo_write", {"todos": bad}), say("x")])
    await h.turn("plan")
    refusal = [m for m in h.session.messages() if m.role == "tool"][0]
    assert refusal.is_error and "todos" in refusal.text


async def test_skill_tool_loads_body_and_lists_available_on_miss(work: Path):
    skill = Skill("deploy", "Ship it", "Step 1: run the pipeline", work / "SKILL.md")
    tool, ctx = SkillTool({"deploy": skill}), make_ctx(work)
    ok = await run_tool(tool, ctx, name="deploy")
    assert "Step 1: run the pipeline" in ok.text
    miss = await run_tool(tool, ctx, name="nope")
    assert miss.is_error and "Available: deploy" in miss.text


# ----------------------------------------------------------------------------- system prompt


def prompt_for(**overrides):
    args = dict(tool_names={"read", "bash", "glob", "task"})
    return build_system_prompt(**{**args, **overrides})


def test_system_prompt_is_static_and_carries_no_environment():
    text = prompt_for()
    assert "Never claim" in text
    assert "Use `read` (not cat)" in text and "`task` to delegate" in text
    assert prompt_for() == text, "must be byte-stable so provider caches hold"
    assert "web_fetch" not in text
    for volatile in ("Working directory:", "Workspace root:", "Sandbox:", "Date:", "Approvals:"):
        assert volatile not in text, f"{volatile!r} belongs in the runtime context"


def test_system_prompt_adds_settings_instructions_and_the_subagent_note():
    text = prompt_for(subagent=True, extra_instructions="Prefer pytest.")
    assert "Prefer pytest." in text and "# Subagent" in text


# ----------------------------------------------------------------------------- subagent


async def test_task_tool_runs_a_child_with_fresh_context_and_returns_its_report(make_harness):
    h = await make_harness(
        [
            call(
                "task",
                {
                    "description": "find config",
                    "prompt": "Locate the config file",
                    "agent": "explore",
                },
            ),
            # --- child turn (shares the scripted adapter) ---
            call("glob", {"pattern": "*.toml"}),
            Reply(text="Found: pyproject.toml", usage=Usage(input_tokens=300, output_tokens=40)),
            # --- parent continues ---
            say("The config is pyproject.toml."),
        ]
    )
    (h.runtime.agent.tool_ctx.cwd / "pyproject.toml").write_text("[x]")
    result = await h.turn("where is the config?")
    assert result.reason == "completed" and result.text == "The config is pyproject.toml."
    child_first = h.adapter.requests[1]
    assert [m.visible_text.strip() for m in child_first.messages] == ["Locate the config file"]
    assert "Permission mode: READ-ONLY" in child_first.messages[0].text
    assert "task" not in {t.name for t in child_first.tools} and "write" not in {
        t.name for t in child_first.tools
    }
    assert "# Subagent" in child_first.system
    report = [m for m in h.session.messages() if m.role == "tool"][0]
    assert report.text == "Found: pyproject.toml" and not report.is_error
    usage_events = [e for e in h.session.events if e.type == "usage"]
    assert len(usage_events) == 1 and usage_events[0].data["usage"]["input_tokens"] > 300 - 1
    children = list(h.session.path.parent.glob("*.jsonl"))
    assert len(children) == 2, "the child keeps its own session log"


async def test_explore_child_cannot_write_even_if_the_model_tries(make_harness, work: Path):
    h = await make_harness(
        [
            call("task", {"description": "try write", "prompt": "write x", "agent": "explore"}),
            call("write", {"file_path": "x.txt", "content": "no"}),  # child: tool is not offered
            say("could not write"),
            say("child reported failure"),
        ]
    )
    await h.turn("delegate")
    assert not (work / "x.txt").exists()
    child_result = h.adapter.requests[2].messages[-1]
    assert child_result.is_error and "Unknown tool 'write'" in child_result.text


async def test_failed_child_is_reported_as_an_error_with_partial_text(make_harness):
    h = await make_harness(
        [
            call("task", {"description": "x", "prompt": "do it"}),
            Reply(text="half a thought", finish="max_tokens"),
            say("I will do it myself"),
        ]
    )
    await h.turn("delegate")
    result = [m for m in h.session.messages() if m.role == "tool"][0]
    assert result.is_error and "did not finish (max_tokens" in result.text
    assert "half a thought" in result.text


async def test_two_tasks_in_one_message_both_complete_with_ordered_results(make_harness):
    h = await make_harness(
        [
            calls(
                ("task", {"description": "a", "prompt": "A", "agent": "explore"}),
                ("task", {"description": "b", "prompt": "B", "agent": "explore"}),
            ),
            say("report A"),
            say("report B"),
            say("both done"),
        ]
    )
    await h.turn("two jobs")
    tool_results = [m for m in h.session.messages() if m.role == "tool"]
    assert len(tool_results) == 2
    assert {m.text for m in tool_results} == {"report A", "report B"}
    assistant = [m for m in h.session.messages() if m.role == "assistant"][0]
    assert [m.tool_call_id for m in tool_results] == [c.id for c in assistant.tool_calls]

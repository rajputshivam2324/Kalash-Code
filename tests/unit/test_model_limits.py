"""Model capabilities and quota failures must not rewrite harness quality."""

import pytest

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.diagnose import diagnose, explain
from kalash.models.limits import DEFAULT_MAX_OUTPUT, describe, lookup, supports_tools
from kalash.models.normalize import Message, Role, TextBlock
from kalash.runtime.context import ContextAssembler
from kalash.runtime.toolhost import ToolHost
from kalash.tools.builtins import default_registry
from kalash.tools.schema import minify_schema

# Verbatim from the failing runs.
ERR_MAX_OUTPUT = (
    "Error code: 400 - {'error': {'message': \"'max_completion_tokens' must be "
    "less than or equal to 4096, the maximum value for 'max_completion_tokens' "
    "is less than the 'context_window' for this model\", 'type': "
    "'invalid_request_error', 'param': 'max_completion_tokens'}}"
)
ERR_TPM = (
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-20b` in organization `org_01k` service tier `on_demand` on "
    "tokens per minute (TPM): Limit 8000, Requested 10692, please reduce your "
    "message size and try again.', 'type': 'tokens', 'code': "
    "'rate_limit_exceeded'}}"
)
ERR_NO_TOOLS = (
    "Error code: 400 - {'error': {'message': \"'tool calling' is not supported "
    "with this model\", 'type': 'invalid_request_error', 'param': 'tool calling'}}"
)


class TestDiagnoseRealErrors:
    def test_max_output_error_yields_the_exact_cap(self):
        result = diagnose(ERR_MAX_OUTPUT, "openai/gpt-oss-20b")
        assert result.kind == "max_output"
        assert result.adjust_max_output == 4096
        assert result.retryable
        assert "4,096" in result.summary

    def test_tpm_error_reports_the_overage(self):
        result = diagnose(ERR_TPM, "openai/gpt-oss-20b")
        assert result.kind == "tpm"
        assert "8,000" in result.summary
        assert "10,692" in result.summary
        assert "2,692 over" in result.summary
        assert result.reduce_input

    def test_no_tools_error_tells_the_user_to_switch(self):
        result = diagnose(ERR_NO_TOOLS, "groq/compound")
        assert result.kind == "no_tools"
        assert "does not support tool calling" in result.summary
        assert "/models" in result.remedy

    @pytest.mark.parametrize("raw", [ERR_MAX_OUTPUT, ERR_TPM, ERR_NO_TOOLS])
    def test_explanation_hides_the_json_wrapper(self, raw):
        text = explain(raw, "m")
        assert "{'error'" not in text
        assert "org_01k" not in text
        assert len(text) < 260

    def test_auth_and_context_errors(self):
        assert diagnose("401 unauthorized: invalid api key").kind == "auth"
        assert diagnose("maximum context length exceeded").kind == "context"

    def test_unknown_error_keeps_the_provider_sentence(self):
        raw = "Error code: 500 - {'error': {'message': 'upstream exploded'}}"
        assert diagnose(raw).summary == "upstream exploded"

    def test_empty_error_does_not_crash(self):
        assert diagnose("").summary

    def test_invalid_tool_call_is_recoverable(self):
        raw = (
            "Tool call validation failed: attempted to call tool 'print_tree' "
            "which was not in request.tools"
        )
        result = diagnose(raw, "openai/gpt-oss-safeguard-20b")
        assert result.kind == "invalid_tool"
        assert result.retryable
        assert "print_tree" in result.summary
        assert "print_tree" in result.remedy


class TestModelCapabilities:
    def test_unknown_model_has_no_quota_policy(self):
        limits = lookup("unknown")
        assert limits.max_output_tokens == DEFAULT_MAX_OUTPUT
        assert not hasattr(limits, "tokens_per_minute")
        assert "TPM" not in describe("unknown")

    def test_explicit_tool_support(self):
        assert not supports_tools("groq/compound")
        assert supports_tools("anthropic/claude-sonnet-4-5")

    def test_schema_preserves_constraints_and_descriptions(self):
        description = "Required constraint. " * 20
        raw = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "title": "Path",
                    "type": "string",
                    "description": description,
                    "default": "src",
                }
            },
        }
        result = minify_schema(raw)
        assert result["additionalProperties"] is False
        assert result["properties"]["path"] == {
            "type": "string",
            "description": description,
            "default": "src",
        }

    def test_tool_set_does_not_shrink_for_a_throughput_limited_model(self, tmp_path):
        registry = default_registry()
        host = ToolHost(registry, EventBus(), "s", tmp_path, model_id="openai/gpt-oss-20b")
        names = {item["name"] for item in host.schemas()}
        assert names == {tool.name for tool in registry.list_tools()}

    async def test_lifetime_spend_does_not_drop_skills_or_instructions(self):
        budget = BudgetState(max_tokens=100_000, tokens_used=99_000)
        assembler = ContextAssembler(budget, 32_768)
        messages = await assembler.assemble(
            system_identity="core contract",
            tool_schemas=[],
            skills_catalog=[{"name": "review", "description": "Read code"}],
            kalash_md_chain=["Never modify legacy/"],
            memory_blocks=[],
            recent_turns=[],
            current_message=Message(Role.USER, [TextBlock(text="continue")]),
        )
        text = messages[0].content[0].text
        assert "Never modify legacy/" in text
        assert "review" in text


class TestSandboxNetworkDecision:
    @pytest.mark.parametrize(
        "command",
        [
            "npm create vite@latest app -- --template react",
            "npm install",
            "pnpm add react",
            "pip install requests",
            "uv sync",
            "cargo build",
            "git clone https://github.com/x/y",
            "curl https://example.com",
            "cd frontend && npm ci",
            "npx tsc --noEmit && npm test",
        ],
    )
    def test_network_commands_are_granted_network(self, command):
        from kalash.permissions.classify import classify_tool_call
        from kalash.runtime.toolhost import _needs_network

        risk = classify_tool_call("shell", {"command": command})
        assert _needs_network(risk), f"{command} would hang without network"

    @pytest.mark.parametrize(
        "command", ["ls -la", "cat package.json", "rg pattern src", "wc -l x.py"]
    )
    def test_local_commands_stay_isolated(self, command):
        from kalash.permissions.classify import classify_tool_call
        from kalash.runtime.toolhost import _needs_network

        risk = classify_tool_call("shell", {"command": command})
        assert not _needs_network(risk)

    def test_fetch_and_search_tools_get_network(self):
        from kalash.permissions.classify import classify_tool_call
        from kalash.runtime.toolhost import _needs_network

        for name in ("fetch", "web_search"):
            assert _needs_network(classify_tool_call(name, {"url": "https://x"}))

    def test_unshare_net_tracks_the_decision(self, tmp_path):
        import shutil
        import sys

        if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
            pytest.skip("bwrap not available")

        from kalash.tools.base import ToolContext
        from kalash.tools.shell import _wrap_sandboxed

        for allow in (False, True):
            ctx = ToolContext(
                session_id="s",
                run_id="r",
                cwd=tmp_path,
                writable_roots=(tmp_path,),
                allow_network=allow,
            )
            argv, wrapped, _warning = _wrap_sandboxed(["/bin/sh", "-c", "true"], ctx, tmp_path)
            if not wrapped:
                pytest.skip("sandbox preflight declined on this host")
            assert ("--unshare-net" in argv) is (not allow)

    def test_dev_is_a_devtmpfs_not_a_readonly_bind(self, tmp_path):
        """A read-only /dev makes `>/dev/null` fail in most build scripts."""
        import shutil
        import sys

        if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
            pytest.skip("bwrap not available")

        from kalash.sandbox.linux import LinuxSandbox
        from kalash.sandbox.policy import SandboxMode, SandboxPolicy

        argv = LinuxSandbox(
            policy=SandboxPolicy(mode=SandboxMode.WORKSPACE_WRITE, workspace_root=tmp_path)
        ).wrap_command(["/bin/sh", "-c", "true"], cwd=str(tmp_path))

        assert "--dev" in argv
        assert not any(
            argv[i] == "--ro-bind" and argv[i + 1] == "/dev" for i in range(len(argv) - 1)
        )

    @pytest.mark.asyncio
    async def test_dev_null_redirect_works(self, tmp_path):
        from kalash.tools.base import ToolContext
        from kalash.tools.shell import ShellParams, ShellTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=tmp_path, writable_roots=(tmp_path,))
        env = await ShellTool().execute(
            ShellParams(command="echo hidden >/dev/null && echo visible", timeout=20),
            ctx,
        )
        assert env.ok, env.error.message if env.error else ""
        assert "visible" in env.content
        assert "/dev" not in (env.content or "")

    def test_preflight_is_cached_and_reports(self):
        from kalash.sandbox.manager import preflight_ok, preflight_report

        first = preflight_ok()
        assert preflight_ok() is first
        assert preflight_report()

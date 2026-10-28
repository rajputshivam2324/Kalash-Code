"""exit_plan_mode: the model hands a finished plan to the user for a decision."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from kalash.tools.base import Params, Tool, ToolContext, ToolResult


class ExitPlanParams(Params):
    plan: str = Field(
        description="The complete plan in Markdown: what you will change, in which files, in "
        "what order, and how you will verify it."
    )


class ExitPlanModeTool(Tool):
    name = "exit_plan_mode"
    description = (
        "Use only in plan mode, when your research is finished: present the complete plan to "
        "the user for approval. If approved, plan mode ends and you can start implementing. "
        "If the user wants changes you will get their feedback; revise and call this again."
    )
    params = ExitPlanParams

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return "exit_plan_mode"

    async def run(self, params: ExitPlanParams, ctx: ToolContext) -> ToolResult:
        if not ctx.extras.get("plan_active", lambda: False)():
            return ToolResult(
                "Not in plan mode, so there is no plan to submit. Just continue with the task.",
                is_error=True,
            )
        decide = ctx.extras.get("plan_decision")
        if decide is None:
            return ToolResult(
                "Plan approval needs an interactive session and none is available. Describe "
                "the plan in your final answer instead.",
                is_error=True,
            )
        verdict, feedback = await decide(params.plan)
        if verdict == "keep":
            note = f" The user's feedback: {feedback}" if feedback else ""
            return ToolResult(
                f"The user did not approve the plan yet; stay in plan mode and revise it.{note}",
                is_error=True,
            )
        mode = "auto" if verdict == "auto" else "ask"
        ctx.extras["set_mode"](mode, announce=False)
        return ToolResult(
            f"The user approved the plan. Permission mode is now {mode.upper()}: you may "
            f"make the changes. Implement the plan and verify the result."
        )

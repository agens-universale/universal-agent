"""Use the core subagent specifications, factories and prompt preparation."""

import hashlib
from dataclasses import asdict, replace
from types import SimpleNamespace

from gc_core.execution.arguments_error import WrongFunctionArgumentsException
from gc_core.execution.state import ExecutionState
from gc_core.tools.arguments_validation import (
    parse_and_validate_tool_arguments,
)
from gc_core.tools.subagents.registry import get_subagent_class
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.usage.account import UsageAccount
from temporalio import activity

from gc_ua_temporalio.child_contracts import ChildPlan, PrepareChild
from gc_ua_temporalio.guard_requests import Decisions, JudgeWait, digest
from gc_ua_temporalio.guarded_tools import feedback


def subagent_metadata(runtime, profile):
    entries = {}
    for name, target in profile.subagents.items():
        cls = get_subagent_class(name)
        if cls is None or target not in runtime.config.profiles:
            raise ValueError(f"Unknown subagent/profile: {name}/{target}")
        child = runtime.config.profiles[target]
        if child.subagents:
            raise ValueError(
                "Temporal profile supports one level of sequential subagents"
            )
        if child.workspace != profile.workspace:
            raise ValueError("Subagent must use the parent's workspace")
        spec = cls.get_agent_specification()
        entries[name] = {
            "spec": spec,
            "description": spec["description"],
            "child_profile": target,
        }
    return entries


def initialize_child(state, ctx, seed):
    """Same role rendering and assignment framing as the local SubagentRunner."""
    runner = SubagentRunner(ctx, state, ctx.config_dict)
    state.chat_completions_system_prompt = (
        runner._resolve_subagent_system_prompt()
    )
    state.shared.working_memory = dict(seed["memory"])
    runner._build_initial_messages(
        state,
        seed["role"],
        {
            "id": "child-assignment",
            "role": "user",
            "content": seed["assignment"],
        },
    )
    # SessionEngine persists the user message once through its normal mailbox.
    state.messages = state.messages[:-1]


class ChildActivities:
    def __init__(self, activities):
        self.activities = activities
        self.runtime = activities.runtime

    @activity.defn(name="ua.prepare_child")
    async def prepare(self, data: PrepareChild) -> ChildPlan:
        return await self.activities._recorded(
            data,
            f"{data.tool.invocation.operation_id}:child:{data.sequence}",
            ChildPlan,
            self._prepare,
        )

    async def _prepare(self, data):
        parent, _ = self.runtime.profile(
            data.parent.profile, data.parent.profile_revision
        )
        name = data.tool.invocation.name
        target = parent.subagents[name]
        profile = self.runtime.config.profiles[target]
        _, original = self.runtime.profile(target, profile.revision)
        state = ExecutionState.from_execution_snapshot(data.state)
        state.default_model = profile.model
        cls = get_subagent_class(name)
        try:
            args = parse_and_validate_tool_arguments(
                tool_name=name,
                arguments=data.tool.invocation.arguments,
                tool_spec=cls.get_agent_specification(),
                allow_empty_for_empty_schema=False,
            )
        except WrongFunctionArgumentsException as exc:
            return ChildPlan(
                result=self.activities._invalid_arguments(exc, data.tool)
            )
        ctx = SimpleNamespace(**vars(original))
        fingerprint = digest(asdict(replace(data, sequence=0, judges=[])))
        ctx.operations = Decisions(fingerprint, (), data.judges)
        instance = await cls.create_instance(
            **args, model=profile.model, config_dict=ctx.config_dict
        )
        try:
            await instance.afinalize_role(ctx, state)
        except JudgeWait as wait:
            return ChildPlan(
                judge=wait.request,
                judge_digest=wait.digest,
                model_profile=target,
                model_revision=profile.revision,
            )
        role, prompt = instance.get_initial_data()
        spent = state.usage.delta_from(
            UsageAccount.from_meta(state.run_start_usage_meta)
        ).totals()["total"]
        budget = min(
            profile.session_token_limit, parent.session_token_limit - spent
        )
        calls = min(
            profile.max_tool_calls, parent.max_tool_calls - state.tool_calls
        )
        if budget <= 0 or calls <= 0:
            return ChildPlan(
                result=feedback(
                    "budget_exhausted", "Parent budget is exhausted"
                )
            )
        thread = (
            "child-"
            + hashlib.sha256(
                f"{data.owner}/{data.tool.invocation.operation_id}".encode()
            ).hexdigest()[:40]
        )
        return ChildPlan(
            start=replace(
                data.parent,
                thread_id=thread,
                prompt=prompt,
                profile=target,
                profile_revision=profile.revision,
                auto_close=True,
                checkpoint=None,
                session_owner=None,
                resume_imported=False,
                initial_message_id=None,
                child_seed={
                    "role": role,
                    "assignment": prompt,
                    "memory": state.shared.working_memory,
                    "max_tool_calls": calls,
                    "session_token_limit": budget,
                },
            )
        )

"""Replay existing guard flows in short activities; waits belong to workflow."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from gc_core.execution.contracts import ToolResult
from gc_core.execution.tool_policy import COMMAND_TOOLS, FILE_EFFECT_TOOLS
from gc_core.tools import _prepare_tool_call, aapply_tool_result
from gc_core.usage.account import UsageAccount
from temporalio.converter import value_to_type

from gc_ua_temporalio.control_contracts import EffectResult, PreparedTool
from gc_ua_temporalio.download import download_destination
from gc_ua_temporalio.guard_requests import (
    Decisions,
    EffectPlanned,
    HumanWait,
    JudgeWait,
    PlanningCommandBackend,
    digest,
)
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.guarded_file import (
    CheckedFileBackend,
    FileEffectUnknown,
    PlanningFileBackend,
    file_facts,
    workspace_lock,
)
from gc_ua_temporalio.storage.sqlite import ProjectionConflict


def feedback(kind, detail):
    return ToolResult(
        json.dumps(
            {"ok": False, "error": {"type": kind, "message": detail}},
            ensure_ascii=False,
        ),
        outcome="tool_error",
    )


class GuardedTools:
    def __init__(self, runtime):
        self.runtime = runtime
        self.ledger = EffectLedger(runtime.store)
        self.locks = Path(runtime.config.artifacts) / "workspace-locks"

    def _context(self, data, decisions, backend=None, cached_content=None):
        _, original = self.runtime.profile(data.profile, data.revision)
        ctx = SimpleNamespace(**vars(original))
        ctx.confirm_request = decisions.request
        ctx.ask_human = decisions.ask
        ctx.os_backend = backend or original.os_backend
        ctx.operations = decisions
        if data.invocation.name == "download_file":
            ctx.download_publish = backend.write_bytes
            ctx.download_content = cached_content
        return ctx

    def _inputs(self, data):
        profile, ctx = self.runtime.profile(data.profile, data.revision)
        arguments = self.runtime._tool_arguments(
            data.invocation, ctx.workspace.root
        )
        state = SimpleNamespace(
            shared=SimpleNamespace(
                working_memory=data.invocation.state.working_memory
            ),
            session_todo=data.invocation.state.session_todo,
            usage=UsageAccount(),
            default_model=profile.model,
            tools=[],
            messages=[],
            call_metadata={},
        )
        # Use the same schema validation/normalization as the legacy dispatcher.
        prepared = _prepare_tool_call(
            data.invocation.name,
            arguments,
            None,
            ctx,
            state=state,
            call_id=data.invocation.call_id,
        )
        if isinstance(prepared, str):
            return (
                profile,
                ctx,
                arguments,
                state,
                feedback("unsupported_tool", prepared),
            )
        _, arguments, _ = prepared
        return profile, ctx, arguments, state, None

    async def _call(self, data, arguments, state, ctx):
        return await aapply_tool_result(
            data.invocation.name,
            arguments,
            ctx=ctx,
            state=state,
            call_id=data.invocation.call_id,
        )

    def _planning_inputs(self, data, profile, original, arguments):
        if data.invocation.name in FILE_EFFECT_TOOLS:
            return self._file_inputs(data, profile, original, arguments)
        if data.invocation.name in COMMAND_TOOLS:
            return self._command_inputs(profile, original, arguments)
        return {}, None, None

    @staticmethod
    def _file_inputs(data, profile, original, arguments):
        if arguments.get("backup") or arguments.get("make_dirs"):
            return PreparedTool(
                "",
                result=feedback(
                    "unsupported_option",
                    "Durable file effects require backup=false and make_dirs=false",
                ),
            )
        try:
            target = (
                download_destination(original, arguments)
                if data.invocation.name == "download_file"
                else arguments["path"]
            )
            before = file_facts(target, profile.workspace)
        except (ValueError, OSError) as exc:
            return PreparedTool(
                "", result=feedback("write_precondition", str(exc))
            )
        path = before["path"]
        if data.invocation.name != "download_file":
            arguments["path"] = path
        return before, path, PlanningFileBackend(original.os_backend, path)

    @staticmethod
    def _command_inputs(profile, original, arguments):
        if (
            arguments.get("run_in_background")
            or arguments.get("timeout", 120) > 180
        ):
            return PreparedTool(
                "",
                result=feedback(
                    "unsupported_option",
                    "Use foreground execution with timeout <= 180 seconds",
                ),
            )
        cwd = Path(arguments.get("cwd") or profile.workspace).resolve()
        root = Path(profile.workspace).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            return PreparedTool(
                "",
                result=feedback(
                    "cwd_invalid",
                    "cwd must be an existing workspace directory",
                ),
            )
        info = cwd.stat()
        before = {
            "root": [str(root)],
            "cwd": [str(cwd), info.st_dev, info.st_ino],
        }
        arguments["cwd"] = str(cwd)
        return before, None, PlanningCommandBackend(original.os_backend)

    async def prepare(self, value, *, cached_content=None):
        data = value.tool
        profile, original, arguments, state, rejected = self._inputs(data)
        if rejected:
            return PreparedTool("", result=rejected)
        planned = self._planning_inputs(data, profile, original, arguments)
        if isinstance(planned, PreparedTool):
            return planned
        before, path, backend = planned
        fingerprint = digest(
            [data.owner, asdict(data.invocation), profile.model_dump(), before]
        )
        decisions = Decisions(fingerprint, value.decisions, value.judges)
        ctx = self._context(data, decisions, backend, cached_content)
        try:
            result = await self._call(data, arguments, state, ctx)
        except HumanWait as wait:
            return PreparedTool(
                fingerprint,
                request=wait.request,
                request_digest=wait.digest,
                path=path,
                before=before,
            )
        except JudgeWait as wait:
            return PreparedTool(
                fingerprint,
                judge=wait.request,
                judge_digest=wait.digest,
                before=before,
            )
        except EffectPlanned as planned:
            return PreparedTool(
                fingerprint,
                before=before,
                effect_kind="command",
                effect_call=planned.call,
            )
        plan = PreparedTool(
            fingerprint, result=result, path=path, before=before
        )
        if (
            isinstance(backend, PlanningFileBackend)
            and backend.content is not None
        ):
            plan.content = self.runtime.artifacts.put(
                backend.content, "application/octet-stream"
            )
        return plan

    async def execute(self, value):
        data = value.prepared.tool
        plan = value.plan
        profile, original, arguments, state, rejected = self._inputs(data)
        if rejected or not (plan.content or plan.effect_call):
            raise ValueError("Effect requires a prepared operation")
        with workspace_lock(self.locks, profile.workspace):
            existing = self.ledger.get(
                data.owner, data.invocation.operation_id
            )
            if existing:
                return self._existing(existing, plan)
            if (
                plan.path
                and file_facts(plan.path, profile.workspace) != plan.before
            ):
                return EffectResult(
                    "replan", detail="File changed since approval"
                )
            # Re-run the same guard chain, using only decisions bound to this plan.
            content = (
                self.runtime.artifacts.read(plan.content)
                if plan.content
                else None
            )
            fresh = await self.prepare(value.prepared, cached_content=content)
            if (
                fresh.request
                or fresh.judge
                or fresh.fingerprint != plan.fingerprint
                or fresh.content != plan.content
                or fresh.effect_call != plan.effect_call
            ):
                return EffectResult(
                    "replan",
                    detail="Guard decision or prepared content changed",
                )
            self.ledger.begin(
                data.owner,
                data.invocation.operation_id,
                str(Path(profile.workspace).resolve()),
                plan.fingerprint,
                asdict(plan),
            )
            backend = self._effect_backend(original, plan, profile)
            ctx = self._context(
                data,
                Decisions(
                    plan.fingerprint,
                    value.prepared.decisions,
                    value.prepared.judges,
                ),
                backend,
                content,
            )
            if plan.path and data.invocation.name != "download_file":
                arguments["path"] = plan.path
            if plan.effect_kind == "command":
                arguments["cwd"] = plan.before["cwd"][0]
            try:
                result = await self._call(data, arguments, state, ctx)
                await self.after_effect(data)
            except (FileEffectUnknown, HumanWait, JudgeWait, EffectPlanned):
                self.ledger.finish(
                    data.owner, data.invocation.operation_id, "UNKNOWN"
                )
                return EffectResult(
                    "unknown", detail="Effect outcome needs reconciliation"
                )
            except BaseException:
                self.ledger.finish(
                    data.owner, data.invocation.operation_id, "UNKNOWN"
                )
                raise
            result = self.runtime._store_large_output(
                result, data, profile.inline_output_chars
            )
            self.ledger.finish(
                data.owner,
                data.invocation.operation_id,
                "SUCCEEDED",
                asdict(result),
            )
            return EffectResult("complete", result=result)

    def _effect_backend(self, original, plan, profile):
        if plan.effect_kind == "command":
            return PlanningCommandBackend(
                original.os_backend, plan.effect_call
            )
        return CheckedFileBackend(
            original.os_backend,
            plan,
            profile.workspace,
            self.runtime.artifacts.read(plan.content),
        )

    async def after_effect(self, data):
        """Fault-injection boundary: effect happened, receipt not committed yet."""

    @staticmethod
    def _existing(row, plan):
        if row["digest"] != plan.fingerprint:
            raise ProjectionConflict(
                "Effect plan changed for an existing operation"
            )
        result = EffectLedger.result(row)
        if result is not None:
            return EffectResult("complete", value_to_type(ToolResult, result))
        return EffectResult(
            "unknown", detail="An earlier effect attempt has no receipt"
        )

    async def reconcile(self, value):
        data = value.prepared.tool
        profile, _ = self.runtime.profile(data.profile, data.revision)
        try:
            with workspace_lock(self.locks, profile.workspace):
                return self._reconcile_locked(
                    data, value.plan, profile.workspace, value.resolution
                )
        except (OSError, ValueError, ProjectionConflict) as exc:
            return EffectResult("unknown", detail=type(exc).__name__)

    def _reconcile_locked(self, data, plan, workspace, resolution=None):
        row = self.ledger.get(data.owner, data.invocation.operation_id)
        if not row:
            # Fence an attempt that timed out before entering the resource lock.
            # A late worker must see the tombstone instead of starting the write.
            self.ledger.begin(
                data.owner,
                data.invocation.operation_id,
                str(Path(workspace).resolve()),
                plan.fingerprint,
                asdict(plan),
            )
            result = feedback(
                "not_applied", "Execution never acquired the effect fence"
            )
            self.ledger.finish(
                data.owner,
                data.invocation.operation_id,
                "NOT_APPLIED",
                asdict(result),
            )
            return EffectResult("complete", result)
        recorded = self._existing(row, plan)
        if recorded.status == "complete":
            return recorded
        if resolution and resolution.resolution != "inspect":
            return self._resolve_operator(data, resolution)
        if plan.effect_kind != "file":
            self.ledger.finish(
                data.owner, data.invocation.operation_id, "UNKNOWN"
            )
            return EffectResult(
                "unknown",
                detail="Process outcome is unknown. Verify external effects and process termination before explicit resolution; execution will not be repeated.",
            )
        current = file_facts(plan.path, workspace)
        if current.get("digest") == plan.content.digest:
            result = plan.result
            status = "SUCCEEDED"
        elif current == plan.before:
            result = feedback(
                "not_applied",
                "The original file is unchanged; the effect was not applied",
            )
            status = "NOT_APPLIED"
        else:
            self.ledger.finish(
                data.owner, data.invocation.operation_id, "UNKNOWN"
            )
            return EffectResult(
                "unknown",
                detail="File matches neither the original nor the intended content",
            )
        self.ledger.finish(
            data.owner, data.invocation.operation_id, status, asdict(result)
        )
        return EffectResult("complete", result)

    def _resolve_operator(self, data, resolution):
        if (
            resolution.operation_id != data.invocation.operation_id
            or resolution.resolution not in ("applied", "not_applied")
            or not resolution.processes_stopped
            or not resolution.output.strip()
        ):
            raise ValueError("Invalid operator resolution")
        result = ToolResult(
            json.dumps(
                {
                    "ok": resolution.resolution == "applied",
                    "operator_resolution": asdict(resolution),
                },
                ensure_ascii=False,
            )
        )
        self.ledger.finish(
            data.owner,
            data.invocation.operation_id,
            "RESOLVED",
            asdict(result),
        )
        return EffectResult("complete", result)

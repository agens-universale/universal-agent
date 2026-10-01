"""Worker-only construction of real tools, filesystem and provider clients."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from gc_core.backends.local import LocalFsBackend
from gc_core.config_modules.intent_scope import IntentScopeConfig
from gc_core.execution.contracts import ToolResult
from gc_core.execution.local import LocalOperations
from gc_core.execution.output_store import ToolCallOutputRecord
from gc_core.execution_workspace import build_execution_workspace
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.states import State
from gc_core.thread_store import sanitize_system_id, validate_thread_id
from gc_core.tools import _finish_tools_data, aapply_tool_result
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.session.stored_outputs.envelope import (
    dumps_stored_tool_output_payload,
    utf8_byte_length,
)
from gc_core.usage.account import UsageAccount

from gc_ua_temporalio.child_activities import (
    initialize_child,
    subagent_metadata,
)
from gc_ua_temporalio.config import TemporalRuntimeConfig
from gc_ua_temporalio.contracts import BootstrapSnapshot
from gc_ua_temporalio.download import DurableDownload
from gc_ua_temporalio.mcp_bootstrap import discover_mcp
from gc_ua_temporalio.storage.artifacts import ArtifactOutputs, ArtifactStore
from gc_ua_temporalio.storage.profiles import ProfilePins
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore, encode


def payload_digest(value):
    return hashlib.sha256(encode(asdict(value)).encode()).hexdigest()


class WorkerRuntime:
    def __init__(self, config: TemporalRuntimeConfig):
        self.config = config
        self.store = SQLiteThreadStore(config.database)
        self.artifacts = ArtifactStore(config.artifacts)
        self.contexts = {}
        pins = ProfilePins(self.store)
        for name, profile in config.profiles.items():
            if set(profile.mcp_servers) - set(config.mcp_servers):
                raise ValueError(f"Profile {name}: unknown MCP server")
            if not Path(profile.workspace).is_dir():
                raise ValueError(
                    f"Profile {name}: workspace must already exist"
                )
            if profile.api_key_env and not os.environ.get(profile.api_key_env):
                raise ValueError(
                    f"Profile {name}: missing credential environment variable"
                )
            self.contexts[name] = self._context(profile)
            subagent_metadata(self, profile)
        for name, profile in config.profiles.items():
            pins.pin("agent", name, profile)
        for name, profile in config.mcp_servers.items():
            pins.pin("mcp", name, profile)

    @staticmethod
    def _context(profile):
        cfg = profile.core_config()
        cfg["completions_url"] = profile.completions_url
        if profile.api_key_env:
            cfg["chat_completion_headers"] = {
                "Authorization": f"Bearer {os.environ[profile.api_key_env]}"
            }
        cfg["launch_root"] = str(Path(profile.workspace).resolve())
        cfg["read_guard"] = {
            "enabled": True,
            "mode": "deny",
            "allowed_read_roots": [cfg["launch_root"]],
        }
        cfg["write_guard"] = {
            "enabled": True,
            "mode": "deny",
            "extra_paths": [],
        }
        backend = LocalFsBackend()
        workspace = build_execution_workspace(backend, cfg)
        entries = get_builtin_tools_data(cfg)
        entries["download_file"]["tool_fabric"] = DurableDownload
        selected = {name: entries[name] for name in profile.tools}
        tools, specs, descriptions, _ = _finish_tools_data(
            selected, {}, {}, cfg, logger=logging.getLogger("gc_ua_temporalio")
        )
        # Read guard keeps its existing policy. Confirmation is explicitly refused
        # by the first stand instead of falling back to stdin inside an activity.
        return SimpleNamespace(
            config_dict=cfg,
            tools_data=tools,
            tools_spec=specs,
            tools_description=descriptions,
            workspace=workspace,
            os_backend=backend,
            model_router=None,
            auth_connector=None,
            logger=logging.getLogger("gc_ua_temporalio"),
            intent_scope=IntentScopeConfig(),
            on_stream_delta=None,
            auxiliary_llm=SimpleNamespace(
                max_tokens=profile.max_tokens,
                judge_max_tokens=profile.max_tokens,
                completion_kwargs={},
            ),
            ask_human=lambda _question: False,
        )

    def profile(self, name, revision):
        profile = self.config.profiles[name]
        if profile.revision != revision:
            raise ValueError(
                "Profile revision mismatch; keep the old version for in-flight runs"
            )
        return profile, self.contexts[name]

    async def bootstrap(self, request):
        start = request.start
        if start.schema_version != 1 or not start.prompt.strip():
            raise ValueError("Invalid SessionStart")
        profile, ctx = self.profile(start.profile, start.profile_revision)
        validate_thread_id(start.thread_id)
        if sanitize_system_id(start.system_id) != start.system_id:
            raise ValueError("Invalid system_id")
        history = self.store.load_thread_state(
            start.system_id, start.thread_id
        )
        previous_meta = self.store.load_meta(start.system_id, start.thread_id)
        if history.messages and not (
            start.resume_imported and previous_meta.get("temporal_import")
        ):
            raise ValueError(
                "Use a new thread or explicitly resume an offline import"
            )
        epoch = self.store.claim(
            start.system_id, start.thread_id, request.owner
        )
        mcp_metadata = await discover_mcp(self, request)
        child_metadata = subagent_metadata(self, profile)
        state = await State.acreate(
            verbose=False,
            model=profile.model,
            tools_spec=[
                *ctx.tools_spec,
                *(entry["spec"] for entry in mcp_metadata.values()),
                *(entry["spec"] for entry in child_metadata.values()),
            ],
            tools_descr=ctx.tools_description,
            skills_list=[],
            max_steps=profile.max_steps,
            system_prompt_id="temporal-profile-v1",
            use_server_subagents=False,
            thread_id=start.thread_id,
            workspace=ctx.workspace,
            chat_completions_system_prompt=profile.system_prompt,
            # AGENTS/prompt loading occurs only here, never on replay.
            agentsmd_override=True,
            agent_md_path=str(Path(profile.workspace) / "AGENTS.md"),
            workspace_config=ctx.config_dict,
            shared=ThreadMetaMapper.shared_from_meta(previous_meta),
            logger=ctx.logger,
        )
        state.backend_api_mode = "chat_completions"
        state.tool_call_outputs_store.set_max_entries(0)
        self._restore_imported_meta(state, previous_meta)
        instructions = state.call_metadata.get("promptVariables", {}).get(
            "userInstructions", ""
        )
        prompt = profile.system_prompt + (
            "\n\n" + instructions if instructions else ""
        )
        state.messages = [
            {
                "id": "system-prompt",
                "role": "system",
                "content": prompt,
            }
        ]
        state.messages.extend(
            m for m in history.messages if m.get("role") != "system"
        )
        cfg = profile.core_config()
        if start.child_seed:
            initialize_child(state, ctx, start.child_seed)
            cfg.update(
                {
                    key: start.child_seed[key]
                    for key in ("max_tool_calls", "session_token_limit")
                }
            )
            prompt = state.messages[0]["content"]
        meta = {
            **previous_meta,
            "backend_api_mode": "chat_completions",
            "chat_completions_system_prompt_rendered": prompt,
        }
        metadata = {
            name: {k: v for k, v in entry.items() if k != "tool_fabric"}
            for name, entry in ctx.tools_data.items()
        }
        metadata.update(mcp_metadata)
        metadata.update(child_metadata)
        return BootstrapSnapshot(
            state.to_execution_snapshot(),
            cfg,
            metadata,
            meta,
            epoch,
            history.max_seq,
            history=history.messages,
        )

    @staticmethod
    def _restore_imported_meta(state, meta):
        state.init_user_intent_log(meta.get("user_intent_log"))
        state.init_current_user_intent(meta.get("current_user_intent"))
        state.init_user_turn_seq(meta.get("user_turn_seq"))
        state.usage = UsageAccount.from_meta(meta.get("token_usage"))
        state.run_start_usage_meta = state.usage.to_meta()
        state.last_main_prompt_tokens, state.last_main_prompt_message_count = (
            ThreadMetaMapper.read_prompt_token_fields(meta)
        )
        state.init_tool_usage_notice_from_meta(meta)
        state.dialog_steps_offset = int(
            meta.get("dialog_steps_offset", 0) or 0
        )

    async def model(self, data):
        from gc_ua_temporalio.preview import PreviewSink

        profile, original = self.profile(data.profile, data.revision)
        ctx = SimpleNamespace(**vars(original))
        if profile.preview_stream:
            sink = PreviewSink(self, data.owner, data.request.operation_id)
            ctx.config_dict = {**original.config_dict, "tui": {"stream": True}}
            ctx.on_stream_delta = sink.delta
        return await LocalOperations(ctx).model(data.request)

    async def tool(self, data):
        profile, ctx = self.profile(data.profile, data.revision)
        invocation = data.invocation
        state = SimpleNamespace(
            shared=SimpleNamespace(
                working_memory=invocation.state.working_memory
            ),
            session_todo=invocation.state.session_todo,
            tool_call_outputs_store=ArtifactOutputs(
                self.artifacts, data.owner
            ),
        )
        result = await aapply_tool_result(
            invocation.name,
            self._tool_arguments(invocation, ctx.workspace.root),
            ctx=ctx,
            state=state,
            call_id=invocation.call_id,
        )
        return self._store_large_output(
            result, data, profile.inline_output_chars
        )

    @staticmethod
    def _tool_arguments(invocation, root):
        # LocalFsBackend resolves relative names against process cwd. Never chdir
        # a shared worker: fix the argument against this profile before guards/I/O.
        if not invocation.name.startswith("fs_") and invocation.name not in (
            "exec_command",
            "exec_python",
        ):
            return invocation.arguments
        try:
            arguments = (
                json.loads(invocation.arguments)
                if isinstance(invocation.arguments, str)
                else dict(invocation.arguments)
            )
        except (ValueError, TypeError):
            return invocation.arguments
        if isinstance(arguments, dict):
            for key in ("path", "directory", "root", "cwd"):
                if isinstance(arguments.get(key), str) and arguments[key]:
                    arguments[key] = str(Path(root) / arguments[key])
        return arguments

    def _store_large_output(self, result: ToolResult, data, limit):
        name = data.invocation.name
        if len(result.output) <= limit or name in (
            "turn_finish",
            "session_read_stored_tool_output",
        ):
            return result
        record = ToolCallOutputRecord(
            "",
            name,
            data.invocation.call_id or "",
            len(result.output),
            result.output,
        )
        ref = ArtifactOutputs(self.artifacts, data.owner).save(record)
        result.artifacts.append(ref)
        result.output = dumps_stored_tool_output_payload(
            ok=True,
            stored=True,
            output_ref=ref.digest,
            from_original_call_id=record.call_id,
            source_tool=name,
            offset=0,
            limit=limit,
            total_chars=len(record.text),
            total_bytes=utf8_byte_length(record.text),
            truncated=True,
            next_offset=limit,
            content=record.text[:limit],
            hint="Continue with session_read_stored_tool_output using output_ref and next_offset.",
        )
        return result

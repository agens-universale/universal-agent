"""gc-cli as a Harbor EXTERNAL agent.

The gc-cli agent loop runs in-process on the host (not a subprocess). Its synchronous
tool loop runs in a worker thread; Harbor's async `environment` is reached through
HarborBackend, which marshals coroutines onto this adapter's event loop. gc-cli's
filesystem/shell tools are swapped for the harbor pack (use_harbor_tools), so file
edits land inside the task container.

Setup (once): install gc-cli into the harbor runner env so it imports directly:
    uv pip install --python "$(command -v harbor)/../python" -e /path/to/background-agent

Usage:
    harbor run --path <task> \\
        --agent-import-path adapters.gc_cli_external:GcCliExternal \\
        --model openai/MiniMaxAI/MiniMax-M2.7
"""
import asyncio
import os
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Observation,
    ObservationResult,
    Step,
    Trajectory,
)
from harbor.utils.trajectory_utils import format_trajectory_json

# gc-cli is installed as a package into the harbor runner env
# (`uv pip install gc-cli-pub`), so it imports directly — no source-dir / PYTHONPATH.

# EXTERNAL agent: the gc-cli loop runs on the HOST, so the backend is reached at
# 127.0.0.1 directly (NOT host.docker.internal, which only resolves inside a container).
DEFAULT_BACKEND_URL = "http://127.0.0.1:8014/v1/chat/completions"
DEFAULT_MODEL = "MiniMaxAI/MiniMax-M2.7"

# gc-cli chat roles → ATIF step sources. ATIF has no "tool" source: tool results are
# folded into the preceding agent step's `observation` (see _messages_to_steps).
_ROLE_TO_SOURCE = {"system": "system", "user": "user", "assistant": "agent"}


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively overlay `over` onto `base` (nested dicts merge, scalars replace)."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _stringify_content(content: Any) -> str:
    """Flatten an OpenAI chat `content` (str | list[part] | None) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            else:
                parts.append(str(p))
        return "\n".join(x for x in parts if x)
    return str(content)


def _messages_to_steps(messages: list) -> list:
    """Map gc-cli `state.messages` to ATIF `Step`s.

    gc-cli keeps the `v1/responses` shape in `state.messages` regardless of backend
    mode (see processing_responses.py): tool calls are ``{"type": "function_call",
    name, arguments}`` and outputs ``{"type": "function_call_output", output}`` — both
    with ``role: None`` — while plain turns carry ``role`` system/user/assistant. We
    also accept the OpenAI chat ``role: "tool"`` shape defensively.

    Tool outputs fold into the most recent agent step's `observation` (ATIF models
    tool feedback as `observation`, not a standalone step). A `function_call` becomes
    an agent step so its result has somewhere to attach. `source_call_id` is left None
    so no same-step tool_call linkage is required.
    """
    steps: list = []
    last_agent: Step | None = None

    def _attach_observation(text: str) -> bool:
        if last_agent is None:
            return False
        result = ObservationResult(content=text or "", source_call_id=None)
        if last_agent.observation is None:
            last_agent.observation = Observation(results=[result])
        else:
            last_agent.observation.results.append(result)
        return True

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        mtype = m.get("type")

        # Tool result → observation on the last agent step.
        if mtype == "function_call_output" or role == "tool":
            text = _stringify_content(m.get("output") if "output" in m else m.get("content"))
            if _attach_observation(text):
                continue
            source, text = "user", text or "(tool result)"  # orphan → user step
        # Tool call → agent step describing the action.
        elif mtype == "function_call":
            name, args = m.get("name") or "tool", m.get("arguments")
            source = "agent"
            text = f"{name}({args})" if args else f"{name}()"
        else:
            source = _ROLE_TO_SOURCE.get(role, "user")
            text = _stringify_content(m.get("content"))

        step = Step(step_id=len(steps) + 1, source=source, message=text)
        steps.append(step)
        if source == "agent":
            last_agent = step
    return steps


class GcCliExternal(BaseAgent):
    SUPPORTS_ATIF: bool = False

    def __init__(
        self,
        *args,
        api_base: str | None = None,
        session_id: str | None = None,
        gc_cli_config: dict | None = None,
        **kwargs,
    ):
        # Mirror Terminus2: the fleet runner injects api_base (Polar gateway URL) and
        # session_id as constructor kwargs. We accept them so the agent talks to the
        # cluster LLM proxy (Polar) instead of a hardcoded backend.
        #
        # gc_cli_config is a per-experiment override bag for the gc-cli config_dict,
        # passed straight from the harbor agent profile's `agent_kwargs.gc_cli_config`
        # (→ AgentConfig.kwargs → here). It deep-merges over _build_config defaults, so
        # tuning no_tool_call_max_count / sampling / max_steps needs no code or rebuild.
        super().__init__(*args, **kwargs)
        self._api_base = api_base
        self._session_id = session_id
        self._overrides = dict(gc_cli_config or {})

    @staticmethod
    def name() -> str:
        return "gc-cli-external"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        self._environment = environment

    @property
    def _model(self) -> str:
        # Strip only the harbor provider prefix ("openai/"); keep the vendor path
        # ("MiniMaxAI/MiniMax-M2.7") which the backend expects as the full model id.
        m = self.model_name or DEFAULT_MODEL
        if m.startswith("openai/"):
            m = m[len("openai/"):]
        return m

    @property
    def _backend_url(self) -> str:
        # Priority: Polar api_base (from the runner) → env override → local default.
        # gc-cli needs a FULL completions URL; Polar gives the /v1 base (LiteLLM would
        # append the path itself for Terminus, so we append it here for gc-cli).
        if self._api_base:
            base = self._api_base.rstrip("/")
            return base if base.endswith("/chat/completions") else base + "/chat/completions"
        return os.environ.get("GC_CLI_BACKEND_URL", DEFAULT_BACKEND_URL)

    def _build_config(self) -> dict:
        # Sensible MiniMax/fleet-parity defaults; the harbor profile may override any
        # key via agent_kwargs.gc_cli_config (deep-merged below).
        defaults = {
            "backend_api_mode": "chat_completions",
            "completions_url": self._backend_url,
            "model": self._model,
            "chat_completions_max_tokens": 32768,
            "use_max_tokens_param": True,
            "chat_completion_kwargs": {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 40,
                "chat_template_kwargs": {"enable_thinking": False},
                "tool_choice": "auto",
            },
            "use_ssl": False,
            "use_retry": True,
            "use_local_subagents": False,
            "use_harbor_tools": True,
            "system_prompt_id": "v1",
            "no_tool_call_max_count": 3,
            "max_steps": 200,
            "save_logs": False,
            "dump_storage": False,
            "console_trace": False,
        }
        # completions_url/model are derived from runtime (api_base/model_name) — keep
        # them authoritative over any stale profile value.
        merged = _deep_merge(defaults, self._overrides)
        merged["completions_url"] = self._backend_url
        merged["model"] = self._model
        return merged

    def _write_trajectory(self, state: Any) -> int:
        """Dump the gc-cli run as ATIF `trajectory.json`, mirroring Terminus2.

        Writes to ``self.logs_dir`` (harbor's agent_dir), which the trial syncs back
        to the results `agent/` folder. Best-effort: a failure here never fails the run.
        Returns the number of steps written (0 if nothing was dumped).
        """
        try:
            messages = list(getattr(state, "messages", None) or []) if state is not None else []
            steps = _messages_to_steps(messages)
            if not steps:
                return 0
            trajectory = Trajectory(
                session_id=self._session_id,
                agent=Agent(
                    name=self.name(),
                    version=self.version() or "unknown",
                    model_name=self._model,
                ),
                steps=steps,
                final_metrics=FinalMetrics(total_steps=len(steps)),
            )
            path = self.logs_dir / "trajectory.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(format_trajectory_json(trajectory.to_json_dict()))
            self.logger.debug(f"ATIF trajectory dumped to {path}")
            return len(steps)
        except Exception:
            self.logger.error("Failed to write ATIF trajectory.json", exc_info=True)
            return 0

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        from run_context import create_run_context
        from agent_executor import AgentExecutor
        from tools.harbor.backend import HarborBackend

        loop = asyncio.get_running_loop()
        backend = HarborBackend(environment, loop)
        config_dict = self._build_config()

        executor = None

        def _run_loop():
            nonlocal executor
            ctx = create_run_context(config_dict, environment=environment)
            ctx.harbor_backend = backend
            executor = AgentExecutor(ctx)
            executor.run([instruction])
            return executor.get_state()

        # Run the synchronous gc-cli loop off the event loop so backend bridging works,
        # then dump the final message history as ATIF trajectory.json.
        state = None
        try:
            state = await asyncio.to_thread(_run_loop)
        finally:
            # On a Harbor agent timeout (CancelledError) or a backend error the worker
            # thread is abandoned before _run_loop returns, so `state` stays None. Recover
            # whatever the executor reached (get_state() is lock-guarded) and still dump a
            # partial ATIF trajectory — otherwise timed-out/errored runs are a black hole
            # with no trace to debug. The original exception re-raises after this block.
            # ponytail: the orphaned thread can't be killed; partial state is best-effort.
            if state is None and executor is not None:
                state = executor.get_state()
            n_steps = self._write_trajectory(state)

            # Populate AgentContext so the run isn't a black box. gc-cli does not track
            # token usage in `state`, so token/cost counters stay None (would need core
            # accounting); metadata at least records the run shape for log triage.
            if context is not None and context.metadata is None:
                context.metadata = {
                    "agent": self.name(),
                    "session_id": self._session_id,
                    "total_steps": n_steps,
                }

"""gc-cli as a Harbor EXTERNAL agent.

The gc-cli agent loop runs in-process on the host (not a subprocess). Its synchronous
tool loop runs in a worker thread; Harbor's async `environment` is reached through
HarborBackend, which marshals coroutines onto this adapter's event loop. gc-cli's
filesystem/shell tools use ``ctx.os_backend`` (``HarborBackend`` when ``os_backend: harbor``
in config), so file edits land inside the task container. ``download_file`` is omitted
when ``os_backend`` is ``harbor`` (host-only tool).

Setup (once): install gc-cli into the harbor runner env so it imports directly:
    uv pip install --python "$(command -v harbor)/../python" -e /path/to/background-agent

Usage:
    harbor run --path <task> \\
        --agent-import-path adapters.gc_cli_external:GcCliExternal \\
        --model openai/MiniMaxAI/MiniMax-M2.7
"""
import asyncio
import os
from pathlib import Path
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

from adapters.gc_cli_config import build_gc_cli_config

# gc-cli is installed as a package into the harbor runner env
# (`uv pip install gc-ua`), so it imports directly — no source-dir / PYTHONPATH.
#
# No hardcoded backend URL / model: runner (api_base / --model) → env → profile
# gc_cli_config, else fail fast. The loop runs on the HOST → local backends via
# 127.0.0.1 (not host.docker.internal).

# gc-cli chat roles → ATIF step sources. ATIF has no "tool" source: tool results are
# folded into the preceding agent step's `observation` (see _messages_to_steps).
_ROLE_TO_SOURCE = {"system": "system", "user": "user", "assistant": "agent"}

# Dependency files (prompts/, skills/) are copied next to adapters/ in the image
# (see harbor/Dockerfile) and live at repo-root in editable/local runs. Resolve them
# from __file__ so they work in both layouts, bypassing the source-checkout-only
# default path helpers (which break in the non-editable image install).
_ADAPTER_DIR = Path(__file__).resolve().parent          # .../adapters
_PROMPTS_DIR = _ADAPTER_DIR.parent / "prompts"          # image: /opt/gc-harbor-adapters/prompts
_SKILLS_DIR = _ADAPTER_DIR.parent / "skills"
if not _PROMPTS_DIR.is_dir():                            # editable/local: fall back to repo root
    _repo_root = _ADAPTER_DIR.parent.parent
    _PROMPTS_DIR = _repo_root / "prompts"
    _SKILLS_DIR = _repo_root / "skills"


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
        # runner --model → env → profile. Strip only the provider prefix
        # ("openai/"); the backend expects the full vendor path as the model id.
        m = (
            self.model_name
            or os.environ.get("GC_CLI_MODEL")
            or self._overrides.get("model")
        )
        if not m:
            raise RuntimeError(
                "No model configured: pass --model to the runner, set GC_CLI_MODEL, "
                "or provide gc_cli_config.model in the agent profile."
            )
        if m.startswith("openai/"):
            m = m[len("openai/"):]
        return m

    @property
    def _backend_url(self) -> str:
        # Polar api_base (runner) → env → profile. gc-cli needs a FULL completions
        # URL; Polar gives the /v1 base, so append the path here.
        if self._api_base:
            base = self._api_base.rstrip("/")
            return base if base.endswith("/chat/completions") else base + "/chat/completions"
        url = os.environ.get("GC_CLI_BACKEND_URL") or self._overrides.get("completions_url")
        if not url:
            raise RuntimeError(
                "No LLM backend configured: the runner passed no api_base, "
                "GC_CLI_BACKEND_URL is unset, and the profile has no "
                "gc_cli_config.completions_url. For local runs: "
                "export GC_CLI_BACKEND_URL=http://127.0.0.1:<port>/v1/chat/completions"
            )
        return url

    def _build_config(self) -> dict:
        # schema defaults < bench defaults < profile gc_cli_config < runtime (url/model).
        # See adapters.gc_cli_config — starting from the schema is what keeps a newly
        # added AppConfig key from KeyError-ing every bench run.
        return build_gc_cli_config(
            completions_url=self._backend_url,
            model=self._model,
            prompts_dir=_PROMPTS_DIR,
            skills_dir=_SKILLS_DIR,
            config_dir=str(_ADAPTER_DIR),
            overrides=self._overrides,
        )

    @staticmethod
    def _apply_injected_skills(config_dict: dict, skills_path: str | None) -> None:
        """Configure a single backend-visible skills root.

        Harbor must never retain the adapter's bundled host path: the backend
        can only see container paths. A nonexistent container sentinel produces
        an intentionally empty single-source registry.
        """
        config_dict["skills_data_path"] = skills_path or "/harbor/.gc_no_skills"

    @staticmethod
    def _nudge_instruction(instruction: str, skills_path: str | None) -> str:
        """Append a one-line, model-agnostic hint to discover skills natively."""
        if not skills_path:
            return instruction
        return (
            f"{instruction}\n\n"
            "Task-specific skills are available — use `skills_list` to see them and "
            "`skills_load` to load the relevant one before solving."
        )

    def _write_trajectory(self, state: Any) -> int:
        """Dump the gc-cli run as ATIF `trajectory.json`, mirroring Terminus2.

        Writes to ``self.logs_dir`` (harbor's agent_dir), which the trial syncs back
        to the results `agent/` folder. Best-effort: a failure here never fails the run.
        Returns the number of steps written (0 if nothing was dumped).
        """
        try:
            archived = list(getattr(self, "_evicted_messages", None) or [])
            current = list(getattr(state, "messages", None) or []) if state is not None else []
            # Full trajectory: compaction-evicted steps (via callback) + compacted tail.
            messages = archived + current
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

        loop = asyncio.get_running_loop()
        config_dict = self._build_config()
        skills_dir = getattr(self, "skills_dir", None)
        container_skills = (
            skills_dir if skills_dir and await environment.is_dir(skills_dir) else None
        )
        self._apply_injected_skills(config_dict, container_skills)
        instruction = self._nudge_instruction(instruction, container_skills)

        executor = None
        self._evicted_messages: list = []

        def _on_messages_evicted(dropped: list) -> None:
            # Host-side archive for ATIF dump; core State does not retain these.
            self._evicted_messages.extend(dropped)

        def _run_loop():
            nonlocal executor
            ctx = create_run_context(
                config_dict,
                environment=environment,
                loop=loop,
                on_messages_evicted=_on_messages_evicted,
            )
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

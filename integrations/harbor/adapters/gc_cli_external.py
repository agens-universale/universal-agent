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
import json
import os
import re
import shlex
import threading
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
from adapters.step_timings import StepTimings

# Harbor task transports -> gc-cli transports, 1:1. Harbor normalises `http` to
# `streamable-http` (models/task/config.py), so those three are all we ever see.
_HARBOR_TRANSPORT_MAP = {
    "streamable-http": "stream_http",
    "sse": "sse",
    "stdio": "stdio",
}


# gc-cli is installed as a package into the harbor runner env
# (`uv pip install gc-ua`), so it imports directly — no source-dir / PYTHONPATH.
#
# No hardcoded backend URL / model: runner (api_base / --model) → env → profile
# gc_cli_config, else fail fast. The loop runs on the HOST → local backends via
# 127.0.0.1 (not host.docker.internal).

# gc-cli chat roles → ATIF step sources. ATIF has no "tool" source: tool results are
# folded into the preceding agent step's `observation` (see _messages_to_steps).
_ROLE_TO_SOURCE = {"system": "system", "user": "user", "assistant": "agent"}

# Container root harbor uploads injected skills to (harbor/models/trial/paths.py:41).
# Used when a profile ships skills but the task injected none.
DEFAULT_CONTAINER_SKILLS_ROOT = "/harbor/skills"

# Dependency files (prompts/, skills/) are copied next to adapters/ in the image
# (see harbor/Dockerfile) and live at repo-root in editable/local runs. Resolve them
# from __file__ so they work in both layouts, bypassing the source-checkout-only
# default path helpers (which break in the non-editable image install).
_ADAPTER_DIR = Path(__file__).resolve().parent  # .../adapters
_PROMPTS_DIR = _ADAPTER_DIR.parent / "prompts"  # image: /opt/gc-harbor-adapters/prompts
_SKILLS_DIR = _ADAPTER_DIR.parent / "skills"
if not _PROMPTS_DIR.is_dir():  # editable/local: fall back to repo
    # Корень репозитория ищем ПО МАРКЕРУ, а не фиксированной глубиной: адаптер
    # уже переезжал (harbor/ → integrations/harbor/), и счётчик .parent молча
    # уводил на уровень выше нужного — промпты переставали находиться.
    # Реализация маркера одна на всех — gc_core.bundled_assets. Своя копия тут
    # была слабой («packages/ рядом с pyproject.toml») и в чужом монорепо
    # объявляла корнем ЕГО корень. Импорт локальный, внутри ветки: в образе
    # промпты лежат рядом с адаптерами, эта ветка не выполняется вовсе, и
    # адаптер не обязан тянуть ядро на старте.
    from gc_core.bundled_assets import repo_root_from as _find_repo_root

    _repo_root = _find_repo_root(_ADAPTER_DIR)
    if _repo_root is None:  # не рабочая копия — оставляем как есть
        _repo_root = _ADAPTER_DIR.parent.parent
    # Ассеты принадлежат дистрибутиву gc-core и лежат рядом с его исходниками;
    # в корне репозитория их больше нет.
    _assets = _repo_root / "packages" / "core" / "assets"
    _base = _assets if (_assets / "prompts").is_dir() else _repo_root
    _PROMPTS_DIR = _base / "prompts"
    _SKILLS_DIR = _base / "skills"


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
        # Отмену трайла могут попросить, пока идёт сборка контекста (дискавери
        # до 120 с), — то есть ДО того, как появится executor, которому можно
        # сказать stop_run(). Флаг закрывает это окно; лок нужен потому, что
        # ставит его loop Harbor, а читает рабочий поток агента.
        self._cancel_lock = threading.Lock()
        self._cancelled = False

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
        m = self.model_name or os.environ.get("GC_CLI_MODEL") or self._overrides.get("model")
        if not m:
            raise RuntimeError(
                "No model configured: pass --model to the runner, set GC_CLI_MODEL, "
                "or provide gc_cli_config.model in the agent profile."
            )
        if m.startswith("openai/"):
            m = m[len("openai/") :]
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

    @property
    def _polar_session_id(self) -> str | None:
        """Session id the runner registered with Polar; ``OPENAI_API_KEY`` is a fallback
        only when api_base came too, else a local key would ship as the session id."""
        raw = self._session_id or (self._api_base and os.environ.get("OPENAI_API_KEY"))
        return raw.strip() or None if raw else None

    @staticmethod
    def _apply_polar_session(config_dict: dict, session_id: str | None) -> None:
        """Send the Polar session id on every LLM call: the core sends a state UUID, so
        completions land in an unregistered session and the merge finds no dialog.json."""
        if not session_id:
            return
        headers = dict(config_dict.get("chat_completion_headers") or {})
        headers["x-session-id"] = session_id
        headers.setdefault("Authorization", f"Bearer {session_id}")
        config_dict["chat_completion_headers"] = headers

    def _build_config(self) -> dict:
        # schema defaults < bench defaults < profile gc_cli_config < runtime (url/model).
        # See adapters.gc_cli_config — starting from the schema is what keeps a newly
        # added AppConfig key from KeyError-ing every bench run.
        config = build_gc_cli_config(
            completions_url=self._backend_url,
            model=self._model,
            prompts_dir=_PROMPTS_DIR,
            skills_dir=_SKILLS_DIR,
            config_dir=str(_ADAPTER_DIR),
            overrides=self._overrides,
        )
        self._apply_polar_session(config, self._polar_session_id)
        return config

    @staticmethod
    def _apply_injected_skills(config_dict: dict, skills_path: str | None) -> None:
        """Configure a single backend-visible skills root.

        Harbor must never retain the adapter's bundled host path: the backend
        can only see container paths. A nonexistent container sentinel produces
        an intentionally empty single-source registry.
        """
        config_dict["skills_data_path"] = skills_path or "/harbor/.gc_no_skills"

    @staticmethod
    def _resolve_profile_skill_dirs(skills_root: Path) -> list[Path]:
        """SKILL.md-bearing directories under a runner-local skills root.

        Mirrors the rule the fleet runner applies to task skills
        (`dind_runner._resolve_task_skill_paths`): a SKILL.md directly in the root
        makes the root one skill, otherwise every immediate subdirectory holding a
        SKILL.md is one. Unrelated siblings are skipped rather than rejected —
        harbor's stricter `resolve_skills` would refuse the whole set.
        """
        if not skills_root.is_dir():
            return []
        if (skills_root / "SKILL.md").is_file():
            return [skills_root]
        return [child for child in sorted(skills_root.iterdir()) if child.is_dir() and (child / "SKILL.md").is_file()]

    async def _merge_profile_skills(
        self, environment: BaseEnvironment, container_root: str | None
    ) -> tuple[str, dict[str, str]]:
        """Upload the profile's skills next to harbor-injected ones.

        `upload_dir` is `docker cp <src>/. <target>`, which merges — task skills
        survive. A name already in the container wins: benchmark skills are part of
        the measurement, so the profile copy is skipped rather than overwriting it.
        """
        skills_data_path = self._overrides.get("skills_data_path")
        if not skills_data_path:
            raise RuntimeError(
                "gc_cli_config.skills_data_path is missing or empty — the profile "
                "must set it to a directory on the runner filesystem before calling "
                "_merge_profile_skills."
            )
        local_root = Path(str(skills_data_path))
        if not local_root.is_dir():
            raise RuntimeError(
                f"gc_cli_config.skills_data_path does not exist on the runner: "
                f"{local_root}. It must be a directory on the runner filesystem — "
                "the adapter uploads it into the task container."
            )
        root = container_root or DEFAULT_CONTAINER_SKILLS_ROOT
        await environment.exec(f"mkdir -p {shlex.quote(root)}")
        candidates = self._resolve_profile_skill_dirs(local_root)
        if not candidates:
            self.logger.warning("No SKILL.md found under %s — no profile skills uploaded", local_root)
            return root, {}
        sources: dict[str, str] = {}
        for skill_dir in candidates:
            target = f"{root}/{skill_dir.name}"
            if await environment.is_dir(target):
                sources[skill_dir.name] = "skipped"
                self.logger.warning(
                    "Skill %s already present in %s — profile copy skipped",
                    skill_dir.name,
                    root,
                )
                continue
            await environment.exec(f"mkdir -p {shlex.quote(target)}")
            await environment.upload_dir(skill_dir, target)
            sources[skill_dir.name] = "profile"
        return root, sources

    async def _write_skills_debug(self, ctx: Any, sources: dict[str, str]) -> None:
        """Dump the skills the registry actually loaded to ``logs_dir/skills.json``.

        Authoritative by construction: the listing comes from the same registry and
        backend the agent uses, so an empty set is visible instead of silent. Written
        beside trajectory.json, which harbor syncs into the task results. Best-effort —
        a debug artifact must never fail a run.
        """
        try:
            from gc_core.tools.skills.registry import get_skills_registry

            config_dict = getattr(ctx, "config_dict", {}) or {}
            root = config_dict.get("skills_data_path")
            registry = get_skills_registry(config_dict, backend=ctx.os_backend)
            loaded = sorted(str(s["id"]) for s in await registry.list_skills())
            payload = {
                "skills_root": root,
                # A name in `sources` mapped to "skipped" means the profile's copy was
                # NOT uploaded because the task already provided that name — the copy
                # the agent actually sees still comes from the task, not the profile.
                # Only an explicit "profile" entry means the profile's copy won.
                "loaded": [
                    {
                        "id": sid,
                        "source": "profile" if sources.get(sid) == "profile" else "task",
                    }
                    for sid in loaded
                ],
                "skipped": sorted(k for k, v in sources.items() if v == "skipped"),
                "counts": {
                    "loaded": len(loaded),
                    "profile": sum(1 for v in sources.values() if v == "profile"),
                    "skipped": sum(1 for v in sources.values() if v == "skipped"),
                },
            }
            path = self.logs_dir / "skills.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            counts = payload["counts"]
            # Counters and the root only: runner stdout is buffered whole into the
            # main container's memory, so this line must stay small.
            self.logger.info(
                "skills: %s loaded from %s (profile=%s, skipped=%s)",
                counts["loaded"],
                root,
                counts["profile"],
                counts["skipped"],
            )
        except Exception:
            self.logger.error("Failed to write skills.json", exc_info=True)

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

    def _populate_usage(self, context: Any, state: Any) -> dict[str, Any] | None:
        """Hand `state.usage` totals to Harbor, which otherwise writes nulls.

        Totals cover every LLM call — main loop, summariser, finish gate, subagents.
        `n_input_tokens` includes cache on both sides. `cost_usd` is left unset: vllm
        returns no price, so cost is computed downstream from tokens and the tariff.
        Best-effort like `_write_trajectory` — runs in a `finally` that may be
        unwinding a timeout. Returns the totals for the caller's metadata.
        """
        try:
            usage = getattr(state, "usage", None) if state is not None else None
            totals = usage.totals() if usage is not None else None
            if not totals or not totals["calls"]:
                return totals
            context.n_input_tokens = totals["input"]
            context.n_output_tokens = totals["output"]
            context.n_cache_tokens = totals["cached"]
            if totals["missing_usage_calls"]:
                self.logger.warning(
                    "%d of %d LLM calls returned no usage; token totals are short by those",
                    totals["missing_usage_calls"],
                    totals["calls"],
                )
            return totals
        except Exception:
            self.logger.exception("could not report token usage to Harbor")
            return None

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

    @staticmethod
    def _harbor_mcp_to_config(servers: list, *, logger: Any = None) -> dict:
        """Task-declared MCP servers as gc-cli `mcp-servers` entries.

        The task's own environment is where these servers live, so they are
        tagged `execute_on: os_backend`. The transport is carried through
        unchanged — the locus is a separate axis.
        """
        # The rule for which transports survive a stateless exec lives in the
        # resolver; a local copy would drift the moment a transport is added.
        # gc-cli is imported lazily here, like everywhere else in this adapter.
        from gc_core.tools.connect_mcp.execute_on import OS_BACKEND_TRANSPORTS

        mapped: dict = {}
        for server in servers or []:
            name = getattr(server, "name", None)
            transport = getattr(server, "transport", None)
            if not name or not transport:
                continue
            gc_transport = _HARBOR_TRANSPORT_MAP.get(str(transport))
            if gc_transport is None:
                if logger is not None:
                    logger.warning(
                        "MCP server %r: unsupported harbor transport %r, skipped",
                        name,
                        transport,
                    )
                continue
            key = re.sub(r"[^A-Za-z0-9_]", "_", str(name))
            if key in mapped and logger is not None:
                logger.warning(
                    "MCP server %r collides with an earlier server on key %r after "
                    "sanitising; the earlier one is dropped",
                    name,
                    key,
                )
            # `sse` needs a live session, which a stateless exec cannot hold, so
            # tagging it os_backend would build a config the resolver is
            # guaranteed to reject. It stays on the agent locus: unreachable from
            # there is an honest connection error, not a self-inflicted config one.
            locus = "os_backend" if gc_transport in OS_BACKEND_TRANSPORTS else "agent"
            if locus == "agent" and logger is not None:
                logger.warning(
                    "MCP server %r uses transport %r, which needs a live session "
                    "and cannot run inside the task environment; it will be tried "
                    "from the agent runtime instead",
                    name,
                    transport,
                )
            entry: dict = {
                "transport": gc_transport,
                "server-name": str(name),
                "execute_on": locus,
            }
            if gc_transport == "stdio":
                entry["command"] = str(getattr(server, "command", "") or "")
                entry["args"] = [str(a) for a in (getattr(server, "args", None) or [])]
            else:
                entry["url"] = str(getattr(server, "url", "") or "")
            mapped[key] = entry
        return mapped

    @staticmethod
    def _apply_mcp_servers(config_dict: dict, servers: list, *, logger: Any = None) -> None:
        """Merge task MCP servers into the run config; existing entries win.

        An explicit entry in the agent's own config is a deliberate statement
        about that server (including its locus), so autotagging never overrides
        it.
        """
        mapped = GcCliExternal._harbor_mcp_to_config(servers, logger=logger)
        if not mapped:
            return
        existing = dict(config_dict.get("mcp-servers") or {})
        # An explicit entry wins — including when the user wrote the server's
        # natural name (`user-server`) and the mapper sanitised it to
        # `user_server`. Comparing raw keys would register the sidecar twice and
        # discover it under two tool prefixes.
        claimed = {re.sub(r"[^A-Za-z0-9_]", "_", str(k)) for k in existing}
        fresh = {k: v for k, v in mapped.items() if k not in claimed}
        config_dict["mcp-servers"] = {**fresh, **existing}
        meta = dict(config_dict.get("mcp-meta") or {})
        # Every MCP operation is now an exec round-trip into the container, and a
        # sim-user answer is a full LLM call on the other side.
        meta["request_timeout_sec"] = max(int(meta.get("request_timeout_sec") or 0), 600)
        meta["discovery_timeout_sec"] = max(int(meta.get("discovery_timeout_sec") or 0), 120)
        config_dict["mcp-meta"] = meta

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        from gc_core.agent_executor import AgentExecutor
        from gc_core.run_context import acreate_run_context

        loop = asyncio.get_running_loop()
        config_dict = self._build_config()
        self._apply_mcp_servers(config_dict, getattr(self, "mcp_servers", None) or [], logger=self.logger)
        skills_dir = getattr(self, "skills_dir", None)
        container_skills = skills_dir if skills_dir and await environment.is_dir(skills_dir) else None
        skill_sources: dict[str, str] = {}
        profile_skills = self._overrides.get("skills_data_path")
        if profile_skills:
            container_skills, skill_sources = await self._merge_profile_skills(environment, container_skills)
        self._apply_injected_skills(config_dict, container_skills)
        instruction = self._nudge_instruction(instruction, container_skills)

        executor = None
        self._evicted_messages: list = []
        # Wall-clock split per step: LLM wait vs tool exec.
        timings = StepTimings(self.logs_dir / "timings.jsonl")

        def _on_messages_evicted(dropped: list) -> None:
            # Host-side archive for ATIF dump; core State does not retain these.
            self._evicted_messages.extend(dropped)

        async def _agent_run():
            nonlocal executor
            from gc_core.utils.async_http import aclose_clients
            from gc_core.utils.logging_module import release_run_logger

            ctx = None
            try:
                ctx = await acreate_run_context(
                    config_dict,
                    environment=environment,
                    loop=loop,
                    on_messages_evicted=_on_messages_evicted,
                )
                await self._write_skills_debug(ctx, skill_sources)
                executor = AgentExecutor(ctx)
                # Отмену могли попросить, пока шла дискавери (до 120 с): тогда
                # `stop_run()` звать было НЕ НА ЧЁМ — `executor` ещё не был
                # присвоен, — и поток спокойно уходил в `arun` уже после того,
                # как его отменили. Флаг ставится под тем же локом, что и
                # чтение здесь.
                with self._cancel_lock:
                    if self._cancelled:
                        return executor.get_state()
                await executor.arun([instruction])
                return executor.get_state()
            finally:
                # Пул httpx этого loop закрываем ЗДЕСЬ: `asyncio.run` ниже
                # убьёт loop, а реестр клиентов ключуется по нему. Без этого
                # клиент трайла жил до сборщика и всплывал «Event loop is
                # closed» в чужом месте.
                #
                # Логгер трайла — туда же: Harbor ведёт много трайлов в одном
                # процессе, а `logging` держит каждый логгер до конца процесса
                # вместе с открытым файлом.
                release_run_logger(getattr(ctx, "logger", None))
                await aclose_clients()

        def _run_loop():
            timings.attach()  # sink routing is per thread — bind inside the worker
            return asyncio.run(_agent_run())

        # Агент идёт СВОИМ `asyncio.run` в рабочем потоке — не через портал и не
        # на loop Harbor.
        #
        # Раньше поток был обязателен: файловый API `FsBackend` был синхронным и
        # у `HarborBackend` ждал через `run_coroutine_threadsafe(...).result()`
        # на loop Harbor — с самого этого loop вечный дедлок. Теперь файловый и
        # exec-порты корутинные, и `HarborBackend._aawait` сам выбирает: прямой
        # `await` на loop Harbor или `wrap_future` с чужого loop, — дедлока нет
        # ни там, ни там. Поток оставлен как есть: перенос агента на loop Harbor
        # поменял бы модель отмены ниже, а на стенде она не проверена.
        #
        # Отменяемость не изменилась: future от `run_in_executor` по-прежнему не
        # отменить, поэтому на таймауте зовём `stop_run()` (кооперативный флаг,
        # который проверяет цикл) и даём потоку время выйти.
        state = None
        fut = loop.run_in_executor(None, _run_loop)
        try:
            state = await fut
        except asyncio.CancelledError:
            with self._cancel_lock:
                self._cancelled = True
            if executor is not None:
                executor.stop_run()
            try:
                state = await asyncio.wait_for(asyncio.shield(fut), timeout=30)
            except TimeoutError:
                self.logger.warning(
                    "gc-cli worker thread still running after stop_run(); "
                    "abandoning it and dumping best-effort partial state"
                )
            raise
        finally:
            # On a Harbor agent timeout (CancelledError) or a backend error the worker
            # thread may be abandoned before _run_loop returns, so `state` stays None.
            # Recover whatever the executor reached (get_state() is lock-guarded) and
            # still dump a partial ATIF trajectory — otherwise timed-out/errored runs
            # are a black hole with no trace to debug. The original exception re-raises
            # after this block.
            # ponytail: best-effort only — if stop_run() didn't unblock the thread in
            # time, the thread is still orphaned; raise the grace timeout if this needs
            # a harder kill later.
            if state is None and executor is not None:
                state = executor.get_state()
            n_steps = self._write_trajectory(state)
            # On a timeout the worker is abandoned mid-call: these are the totals
            # reached so far, and timings.jsonl names the call still running.
            timing_summary = timings.summary()
            timings.close()
            self.logger.info("timings: %s", timing_summary)

            # Populate AgentContext so the run isn't a black box: token counters the
            # same way every other Harbor agent reports them, plus metadata for triage.
            if context is not None:
                totals = self._populate_usage(context, state)
                if context.metadata is None:
                    context.metadata = {
                        "agent": self.name(),
                        "session_id": self._session_id,
                        "total_steps": n_steps,
                        "timings": timing_summary,
                        # A zero token count means "no calls"; a missing one means "not
                        # counted". Record both so the difference stays visible.
                        "llm_calls": totals["calls"] if totals else None,
                        "llm_calls_without_usage": (totals["missing_usage_calls"] if totals else None),
                    }

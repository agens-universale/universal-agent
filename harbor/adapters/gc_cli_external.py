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

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# gc-cli is installed as a package into the harbor runner env
# (`uv pip install gc-cli-pub`), so it imports directly — no source-dir / PYTHONPATH.

# EXTERNAL agent: the gc-cli loop runs on the HOST, so the backend is reached at
# 127.0.0.1 directly (NOT host.docker.internal, which only resolves inside a container).
DEFAULT_BACKEND_URL = "http://127.0.0.1:8014/v1/chat/completions"
DEFAULT_MODEL = "MiniMaxAI/MiniMax-M2.7"


class GcCliExternal(BaseAgent):
    SUPPORTS_ATIF: bool = False

    def __init__(self, *args, api_base: str | None = None, session_id: str | None = None, **kwargs):
        # Mirror Terminus2: the fleet runner injects api_base (Polar gateway URL) and
        # session_id as constructor kwargs. We accept them so the agent talks to the
        # cluster LLM proxy (Polar) instead of a hardcoded backend.
        super().__init__(*args, **kwargs)
        self._api_base = api_base
        self._session_id = session_id

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
        return {
            "backend_api_mode": "chat_completions",
            "completions_url": self._backend_url,
            "model": self._model,
            "chat_completions_max_tokens": 32768,
            "use_ssl": False,
            "use_retry": True,
            "use_local_subagents": False,
            "use_harbor_tools": True,
            "system_prompt_id": "v1",
            "save_logs": False,
            "dump_storage": False,
            "console_trace": False,
            "max_steps": 200,
        }

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        from run_context import create_run_context
        from agent_executor import AgentExecutor
        from tools.harbor.backend import HarborBackend

        loop = asyncio.get_running_loop()
        backend = HarborBackend(environment, loop)
        config_dict = self._build_config()

        def _run_loop():
            ctx = create_run_context(config_dict, environment=environment)
            ctx.harbor_backend = backend
            return AgentExecutor(ctx).run([instruction])

        # Run the synchronous gc-cli loop off the event loop so backend bridging works.
        await asyncio.to_thread(_run_loop)

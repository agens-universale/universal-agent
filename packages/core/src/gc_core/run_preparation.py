"""External inputs for a run; the executor consumes the same prepared values."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from gc_core.agent_store import (
    DEFAULT_AGENT_STORE,
    AgentResource,
    AgentStore,
    read_resource_or_none,
)
from gc_core.config_modules.agent import (
    join_special_instructions_with_steering,
    resolve_model_steering_text,
)
from gc_core.settings import settings_of


@dataclass(frozen=True)
class PromptPreparationRequest:
    system_prompt_path: str | None = None
    special_instructions_path: str | None = None
    model_steering: str | None = None
    verbose: bool = False

    @classmethod
    def from_config(cls, settings: Any, *, verbose: bool):
        """``settings`` — всё, что принимает ``settings_of`` (контекст, настройки)."""
        settings = settings_of(settings)

        def path(value):
            return value if isinstance(value, str) and value.strip() else None

        return cls(
            system_prompt_path=path(
                settings.paths.chat_completions_system_prompt_path
            ),
            special_instructions_path=path(
                settings.paths.chat_completions_special_instructions_path
            ),
            model_steering=resolve_model_steering_text(settings),
            verbose=verbose,
        )


@dataclass(frozen=True)
class PreparedPrompts:
    system_prompt: str | None = None
    special_instructions: str | None = None


PromptLoader = Callable[[PromptPreparationRequest], Awaitable[PreparedPrompts]]


def load_run_prompts(
    request: PromptPreparationRequest,
    *,
    logger: Any = None,
    store: AgentStore | None = None,
) -> PreparedPrompts:
    """Read local inputs with the existing required/optional file semantics.

    Local execution calls this directly; an external executor may invoke the
    same handler behind its I/O boundary and return PreparedPrompts. Neither
    the logger nor the full config (which can contain credentials) is payload.
    Тексты отдаёт хранилище агента (по умолчанию — файлы по путям конфига);
    путь не задан — своё хранилище может отдать промпт по имени.
    """
    store = store if store is not None else DEFAULT_AGENT_STORE
    system = AgentResource("prompt", "system", request.system_prompt_path)
    system_prompt = None
    try:
        system_prompt = store.read_text(system).strip()
    except (OSError, UnicodeDecodeError) as exc:
        absent = request.system_prompt_path is None and isinstance(
            exc, FileNotFoundError
        )
        if not absent:
            if request.verbose:
                print(
                    "Не удалось прочитать chat_completions system prompt: "
                    f"{system.label}"
                )
            if logger is not None:
                logger.warning(
                    "system_prompt_read_failed",
                    extra={
                        "event": "system_prompt_read_failed",
                        "path": system.label,
                    },
                )
            raise
    path = request.special_instructions_path
    special_instructions = read_resource_or_none(
        store,
        AgentResource(
            "prompt",
            "special_instructions",
            path.strip() if path is not None else None,
        ),
    )
    return PreparedPrompts(
        system_prompt=system_prompt,
        special_instructions=join_special_instructions_with_steering(
            (special_instructions or "").strip() or None,
            request.model_steering,
        ),
    )

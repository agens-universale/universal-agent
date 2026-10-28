"""External inputs for a run; the executor consumes the same prepared values."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from gc_core.agent_store import (
    AgentStore,
    Prompt,
    aread_or_none,
    decode_text,
)
from gc_core.config_modules.agent import (
    join_special_instructions_with_steering,
    resolve_model_steering_text,
)
from gc_core.settings import settings_of


@dataclass(frozen=True)
class PromptPreparationRequest:
    """Что нужно прогону, кроме самих текстов: их пути знает хранилище
    агента (у файлового — по настройкам)."""

    model_steering: str | None = None
    verbose: bool = False

    @classmethod
    def from_config(cls, settings: Any, *, verbose: bool):
        """``settings`` — всё, что принимает ``settings_of`` (контекст, настройки)."""
        return cls(
            model_steering=resolve_model_steering_text(settings_of(settings)),
            verbose=verbose,
        )


@dataclass(frozen=True)
class PreparedPrompts:
    system_prompt: str | None = None
    special_instructions: str | None = None


PromptLoader = Callable[[PromptPreparationRequest], Awaitable[PreparedPrompts]]


async def aload_run_prompts(
    request: PromptPreparationRequest,
    *,
    store: AgentStore,
    logger: Any = None,
) -> PreparedPrompts:
    """Read local inputs with the existing required/optional file semantics.

    Local execution calls this directly; an external executor may invoke the
    same handler behind its I/O boundary and return PreparedPrompts. Neither
    the logger nor the full config (which can contain credentials) is payload.
    Системного промпта у хранилища нет (путь не задан) — ``None``; есть, но не
    читается — отказ прогона.
    """
    label = store.label("prompt", Prompt.SYSTEM)
    system_prompt = None
    try:
        data = await store.read("prompt", Prompt.SYSTEM)
        if data is not None:
            system_prompt = decode_text(data).strip()
    except (OSError, UnicodeDecodeError):
        if request.verbose:
            print(
                f"Не удалось прочитать chat_completions system prompt: {label}"
            )
        if logger is not None:
            logger.warning(
                "system_prompt_read_failed",
                extra={"event": "system_prompt_read_failed", "path": label},
            )
        raise
    special_instructions = await aread_or_none(
        store, "prompt", Prompt.SPECIAL_INSTRUCTIONS
    )
    return PreparedPrompts(
        system_prompt=system_prompt,
        special_instructions=join_special_instructions_with_steering(
            (special_instructions or "").strip() or None,
            request.model_steering,
        ),
    )

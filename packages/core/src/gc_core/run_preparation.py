"""External inputs for a run; the executor consumes the same prepared values."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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
    request: PromptPreparationRequest, *, logger: Any = None
) -> PreparedPrompts:
    """Read local inputs with the existing required/optional file semantics.

    Local execution calls this directly; an external executor may invoke the
    same handler behind its I/O boundary and return PreparedPrompts. Neither
    the logger nor the full config (which can contain credentials) is payload.
    """
    system_prompt = None
    if request.system_prompt_path is not None:
        try:
            with open(request.system_prompt_path, encoding="utf-8") as fd:
                system_prompt = fd.read().strip()
        except (OSError, UnicodeDecodeError):
            if request.verbose:
                print(
                    "Не удалось прочитать chat_completions system prompt: "
                    f"{request.system_prompt_path}"
                )
            if logger is not None:
                logger.warning(
                    "system_prompt_read_failed",
                    extra={
                        "event": "system_prompt_read_failed",
                        "path": request.system_prompt_path,
                    },
                )
            raise
    special_instructions = None
    if request.special_instructions_path is not None:
        try:
            with open(
                request.special_instructions_path.strip(), encoding="utf-8"
            ) as fd:
                special_instructions = fd.read().strip() or None
        except (OSError, UnicodeDecodeError):
            special_instructions = None
    return PreparedPrompts(
        system_prompt=system_prompt,
        special_instructions=join_special_instructions_with_steering(
            special_instructions, request.model_steering
        ),
    )

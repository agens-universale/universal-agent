from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS = 200_000
DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS = 5_000


class QualityGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_dialogue_chars: int = DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS
    max_message_chars: int = DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS


def normalize_quality_gate_config(cfg: QualityGateConfig) -> None:
    """Санитизация лимитов digest при загрузке конфига (аналог ``summary`` в ``resolve_paths``)."""
    if cfg.max_dialogue_chars <= 0:
        _logger.warning(
            "quality_gate.max_dialogue_chars=%s must be > 0; using default %s",
            cfg.max_dialogue_chars,
            DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS,
        )
        cfg.max_dialogue_chars = DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS
    if cfg.max_message_chars <= 0:
        _logger.warning(
            "quality_gate.max_message_chars=%s must be > 0; using default %s",
            cfg.max_message_chars,
            DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS,
        )
        cfg.max_message_chars = DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS



def digest_limits_from_params(params: dict | None) -> dict[str, int]:
    """
    Лимиты digest из уже нормализованного ``quality_gate_params`` (``config_dict``).

    Ожидается, что значения прошли ``normalize_quality_gate_config`` в ``resolve_paths``.
  """
    if not isinstance(params, dict):
        params = {}
    return {
        "max_dialogue_chars": int(
            params.get("max_dialogue_chars", DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS)
        ),
        "max_message_chars": int(
            params.get("max_message_chars", DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS)
        ),
    }

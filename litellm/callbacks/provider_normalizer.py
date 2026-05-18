"""LiteLLM Proxy dispatcher: pick a vendor-specific module per request.

This file is the integration point referenced by ``proxy_config.yaml``:

    litellm_settings:
      callbacks: callbacks.provider_normalizer.proxy_handler_instance

Routing is by ``data["model"].startswith(...)`` — no central registry of
providers. Each vendor lives in its own module:

  ``callbacks._gigachat`` — 9-walker schema normalization (pre-call) +
                            3-level plain-text rescue (post-call).
  ``callbacks._yandex``    — empty-content backfill (pre-call) +
                            JSON-in-content → tool_calls synth (post-call).

The proxy must be started from inside ``litellm/`` so that ``callbacks``
is importable as a top-level package. ``litellm/run.py`` does this via
``cwd=litellm/``.
"""
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

from callbacks import _gigachat, _yandex

logger = logging.getLogger(__name__)


class ProviderNormalizer(CustomLogger):
    """Pre/post-call hooks applied by LiteLLM Proxy around upstream calls."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: str,
    ) -> dict:
        model = (data.get("model") or "").lower()

        if model.startswith("gigachat"):
            tools = data.get("tools") or []
            stats = _gigachat.normalize_tools(tools)
            logger.info(
                "ProviderNormalizer[gigachat]: tools=%d strict_stripped=%d keywords_stripped=%d",
                stats["tools"], stats["strict_stripped"], stats["keywords_stripped"],
            )
            # Empty content also breaks GigaChat in some assistant-only-tool_calls
            # corner cases (same root cause as Yandex).
            messages = data.get("messages") or []
            patched = _yandex.backfill_empty_content(messages)
            if patched:
                logger.info(
                    "ProviderNormalizer[gigachat]: %d/%d messages patched for empty content",
                    patched, len(messages),
                )

        if model.startswith("yandex"):
            messages = data.get("messages") or []
            patched = _yandex.backfill_empty_content(messages)
            logger.info(
                "ProviderNormalizer[yandex]: %d/%d messages patched for empty content",
                patched, len(messages),
            )

        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict,
        response: Any,
    ) -> Any:
        model = (data.get("model") or "").lower()

        if model.startswith("yandex"):
            tools = data.get("tools") or []
            patched = _yandex.synthesize_tool_calls(response, tools)
            if patched:
                logger.info(
                    "ProviderNormalizer[yandex]: synthesized tool_calls in %d choice(s)",
                    patched,
                )

        if model.startswith("gigachat"):
            tools = data.get("tools") or []
            stats = _gigachat.synthesize_tool_call(response, tools)
            total = (
                stats["l1_fenced_json"]
                + stats["l2_superquote"]
                + stats["l3_submit_wrap"]
            )
            if total:
                logger.info(
                    "ProviderNormalizer[gigachat]: synthesized tool_calls — "
                    "L1_fenced=%d L2_superquote=%d L3_submit_wrap=%d",
                    stats["l1_fenced_json"],
                    stats["l2_superquote"],
                    stats["l3_submit_wrap"],
                )

        return response


# LiteLLM Proxy expects a module-level instance to use as the callback.
proxy_handler_instance = ProviderNormalizer()

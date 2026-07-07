"""Маскировка секрет-подобных значений перед записью в лог (key-name + value-regex).

Standalone-модуль (только stdlib): применяется в JsonlFormatter как единая точка
редактинга для stdout и файла. Не переиспользует tui/services/redact (UI-only).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

_logger = logging.getLogger(__name__)

MASK = "***REDACTED***"

SENSITIVE_KEY_SUBSTRINGS = (
    "authorization",
    "api_key", "api-key", "apikey", "x-api-key",
    "secret", "password", "passwd", "cookie", "set-cookie", "credential",
)

# "token" как отдельное слово-компонент, НО не "tokens"-множественное
# (prompt_tokens / completion_tokens / max_tokens — счётчики/лимиты, не секреты).
# Ловит: token, auth_token, access_token, refresh_token, token_id, x-token.
SENSITIVE_KEY_PATTERNS = (
    re.compile(r"(?:^|[_\-])token(?:$|[_\-])", re.IGNORECASE),
)

# Заякоренные паттерны популярных секретов (низкий false-positive; без энтропийных).
SECRET_VALUE_PATTERNS = (
    # --- HTTP auth ---
    r"(?i)\bbearer\s+[A-Za-z0-9._\-]+",                          # Bearer <token>
    r"(?i)\bbasic\s+[A-Za-z0-9+/=]+",                           # Basic <base64>
    r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",  # JWT (eyJ…)
    # --- GitHub ---
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",                            # ghp_/gho_/ghu_/ghs_/ghr_
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",                          # fine-grained PAT
    # --- OpenAI/OpenRouter/Anthropic (sk-…, sk-ant-…, sk-or-…) ---
    r"\bsk-[A-Za-z0-9_\-]{16,}",
    # --- Stripe (sk_live_/sk_test_/rk_live_…) ---
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",
    # --- AWS access key id ---
    r"\bAKIA[0-9A-Z]{16}\b",
    # --- GCP / Google ---
    r"\bAIza[0-9A-Za-z_\-]{35}\b",                              # Google API key
    r"\bya29\.[0-9A-Za-z_\-]{20,}",                             # Google OAuth access token
    # --- GitLab / npm / Slack ---
    r"\bglpat-[0-9A-Za-z_\-]{20}",                             # GitLab PAT
    r"\bnpm_[A-Za-z0-9]{36}",                                  # npm token
    r"\bxox[baprs]-[0-9A-Za-z-]{10,}",                         # Slack token
    # --- PEM private key block (RSA/EC/DSA/OPENSSH/PGP) ---
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z0-9 ]*PRIVATE KEY-----",
)

_EMPTY = (None, "", {}, [])


class Redactor:
    """Немутирующий редактор секретов: маска по имени ключа и по value-паттернам."""

    def __init__(
        self,
        *,
        extra_keys: Iterable[str] = (),
        extra_patterns: Iterable[str] = (),
    ) -> None:
        self._key_substrings = tuple(SENSITIVE_KEY_SUBSTRINGS) + tuple(
            str(k).lower() for k in (extra_keys or ())
        )
        compiled = [re.compile(p) for p in SECRET_VALUE_PATTERNS]
        for p in (extra_patterns or ()):
            try:
                compiled.append(re.compile(str(p)))
            except re.error as exc:
                _logger.warning(
                    "log_redaction: invalid extra pattern %r skipped: %s", p, exc
                )
        self._patterns = tuple(compiled)

    def _is_sensitive_key(self, key: Any) -> bool:
        k = str(key).lower()
        if any(sub in k for sub in self._key_substrings):
            return True
        return any(pat.search(k) for pat in SENSITIVE_KEY_PATTERNS)

    def _redact_str(self, value: str) -> str:
        for pat in self._patterns:
            value = pat.sub(MASK, value)
        return value

    def redact(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict = {}
            for k, v in obj.items():
                if self._is_sensitive_key(k) and v not in _EMPTY:
                    out[k] = MASK
                else:
                    out[k] = self.redact(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.redact(v) for v in obj]
        if isinstance(obj, str):
            return self._redact_str(obj)
        return obj


def build_redactor(redact_cfg: dict | None) -> "Redactor | None":
    """Собрать Redactor из секции logging.redact_secrets. None — если редактинг выключен."""
    if not redact_cfg or not redact_cfg.get("enabled"):
        return None
    return Redactor(
        extra_keys=redact_cfg.get("extra_keys") or [],
        extra_patterns=redact_cfg.get("extra_patterns") or [],
    )


__all__ = ["MASK", "Redactor", "build_redactor", "SENSITIVE_KEY_SUBSTRINGS", "SECRET_VALUE_PATTERNS"]

"""Общее для литеральных правок файла: fs_apply_file_edits и fs_search_and_replace.

Оба тула одинаково проверяют аргументы диффа/бэкапа/стейл-хеша, одинаково
читают файл (лимит размера → байты → декодирование → сверка SHA-256), одинаково
склеивают замены, строят unified diff и пишут результат в исходной кодировке.
Здесь — ровно эти общие шаги; всё, чем тулы различаются (форма ответа, выбор
вхождений, тексты ошибок поиска), остаётся в их модулях.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict

from gc_core.backends.runtime_probe import RuntimeUnavailable
from gc_core.utils.file_text import (
    check_expected_file_sha256,
    decode_text_bytes,
    is_valid_sha256_hex,
    sha256_hex_of_bytes,
    try_encode_text_bytes,
)
from gc_core.utils.fs_size_limits import check_file_size, fs_mutate_max_bytes


class LoadError:
    """Файл не прочитан: готовые message/error для ответа тула."""

    def __init__(self, message: str, error: Dict[str, Any]) -> None:
        self.message = message
        self.error = error


def diff_arg_error(
    backup,
    return_diff,
    diff_context_lines,
    max_diff_chars,
    expected_file_sha256,
) -> tuple[str, Dict[str, Any]] | None:
    """(message, error) для первого невалидного аргумента бэкапа/диффа/хеша либо None."""
    if not isinstance(backup, bool):
        return "Argument 'backup' must be a boolean.", {
            "type": "invalid_argument",
            "field": "backup",
        }
    if not isinstance(return_diff, bool):
        return "Argument 'return_diff' must be a boolean.", {
            "type": "invalid_argument",
            "field": "return_diff",
        }
    if not isinstance(diff_context_lines, int) or diff_context_lines < 0:
        return (
            "Argument 'diff_context_lines' must be a non-negative integer.",
            {
                "type": "invalid_argument",
                "field": "diff_context_lines",
            },
        )
    if not isinstance(max_diff_chars, int) or max_diff_chars < 1:
        return "Argument 'max_diff_chars' must be a positive integer.", {
            "type": "invalid_argument",
            "field": "max_diff_chars",
        }
    if expected_file_sha256 is not None and (
        not isinstance(expected_file_sha256, str)
        or not is_valid_sha256_hex(expected_file_sha256)
    ):
        return (
            "Argument 'expected_file_sha256' must be a 64-character hex SHA-256 digest.",
            {
                "type": "invalid_argument",
                "field": "expected_file_sha256",
            },
        )
    return None


def build_unified_diff(
    path: str,
    old_content: str,
    new_content: str,
    *,
    context_lines: int,
    max_chars: int,
) -> tuple[str, bool]:
    """(diff, truncated): unified diff, обрезанный до max_chars символов."""
    diff_text = "".join(
        line if line.endswith("\n") else f"{line}\n"
        for line in difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=context_lines,
            lineterm="\n",
        )
    )
    if len(diff_text) <= max_chars:
        return diff_text, False
    return diff_text[:max_chars], True


def splice_replacements(
    content: str, selected: list, search: str, replace: str
) -> str:
    """Заменить выбранные вхождения (start_idx, start_line, end_line) на replace."""
    parts: list[str] = []
    prev_end = 0
    for start_idx, _, _ in sorted(selected, key=lambda x: x[0]):
        parts.append(content[prev_end:start_idx])
        parts.append(replace)
        prev_end = start_idx + len(search)
    parts.append(content[prev_end:])
    return "".join(parts)


async def load_text_source(
    tool,
    path: str,
    *,
    operation: str,
    encoding: str | None,
    expected_file_sha256: str | None,
):
    """(text, encoding_used, raw) либо LoadError с готовыми message/error.

    ``raw`` — прочитанные байты как есть: журнал правок берёт их базой «было».

    ``tool`` — сам тул (AbstractSimpleTool): ``tool._backend`` читается внутри
    ``try`` при каждом обращении, как и раньше в методе тула, — ошибка свойства
    (нет ctx/os_backend) по-прежнему превращается в stat_failed/read_failed.
    """
    try:
        file_size = await tool._backend.getsize(path)
    except RuntimeUnavailable:
        raise
    except Exception as e:
        return LoadError(
            "Failed to get file size.",
            {"type": "stat_failed", "message": str(e)},
        )
    size_err = check_file_size(
        file_size,
        fs_mutate_max_bytes(tool._settings_source),
        operation=operation,
    )
    if size_err is not None:
        return LoadError(size_err["message"], size_err)
    try:
        raw = await tool._backend.read_bytes(path)
    except RuntimeUnavailable:
        raise
    except Exception as e:
        return LoadError(
            "Failed to read file.", {"type": "read_failed", "message": str(e)}
        )
    decoded = decode_text_bytes(raw, encoding=encoding)
    if not decoded.ok:
        err = decoded.error or {
            "type": "read_failed",
            "message": "Failed to decode file.",
        }
        return LoadError(err.get("message", "Failed to decode file."), err)
    stale_error = check_expected_file_sha256(
        sha256_hex_of_bytes(raw), expected_file_sha256
    )
    if stale_error is not None:
        return LoadError(stale_error["message"], stale_error)
    return decoded.text or "", decoded.encoding_used, raw


async def encode_and_write(
    tool, path: str, text: str, encoding_used: str | None
):
    """(bytes, None) либо (None, (message, error)); ``tool._backend`` — внутри ``try``, как было."""
    try:
        out_bytes, encode_err = try_encode_text_bytes(
            text, encoding_used or "utf-8"
        )
        if encode_err is not None:
            return None, (
                encode_err.get("message", "Failed to encode file."),
                encode_err,
            )
        assert out_bytes is not None
        await tool._backend.write_bytes(path, out_bytes)
    except RuntimeUnavailable:
        raise
    except Exception as e:
        return None, (
            "Failed to write file.",
            {"type": "write_failed", "message": str(e)},
        )
    return out_bytes, None

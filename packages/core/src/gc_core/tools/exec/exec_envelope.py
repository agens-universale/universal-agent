"""Общая сборка каналов stdout/stderr для model-facing subprocess-тул.

Используют ``exec_command``, ``exec_python`` и ``skills_run_script``.

Два уровня представления одного и того же наблюдённого потока:

* расширенный канал (``stdout`` / ``stderr``) — весь bounded capture;
* front-loaded ``preview`` — по ``P`` символов настоящего начала и настоящего
  конца, и он идёт в envelope **раньше** расширенных каналов.

Порядок нужен из-за общего ``stored_tool_output``: первая страница длинного
envelope — это его префикс, поэтому модель сначала видит статус, ошибку и края
обоих потоков, а не только начало первого длинного поля.

Повторной обрезки capture здесь нет, ``store.put`` на отдельные потоки — тоже:
доставку ограничивает общий wrapper, единственный ref относится ко всему
результату тулы.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from gc_core.backends.capture import CapturedStream


def stream_to_json(stream: CapturedStream) -> dict:
    """Расширенный канал: ``content`` для целого потока, ``head``/``tail`` для обрезанного.

    ``truncated`` пишется только при ``true``: у целого потока форма уже
    однозначна по наличию ``content``, а ``false`` только шумит в токене.
    """
    if not stream.truncated:
        return {
            "content": stream.content,
            "total_chars": stream.total_chars,
        }
    return {
        "truncated": True,
        "head": stream.head,
        "tail": stream.tail,
        "total_chars": stream.total_chars,
        "omitted_chars": stream.omitted_chars,
    }


def stream_preview_json(
    stream: CapturedStream, preview_edge_chars: int
) -> Optional[dict]:
    """Front-loaded preview потока или ``None``, если он не нужен.

    Не нужен при ``P = 0`` и при ``total_chars <= 2P``: такой канал целиком
    лежит в расширенном поле и дублировать его нечем.
    """
    p = preview_edge_chars
    if p <= 0 or stream.total_chars <= 2 * p:
        return None
    source_head = stream.head if stream.truncated else stream.content
    source_tail = stream.tail if stream.truncated else stream.content
    head = source_head[:p]
    tail = source_tail[-p:] if p else ""
    return {
        "head": head,
        "tail": tail,
        "total_chars": stream.total_chars,
        "omitted_chars": stream.total_chars - len(head) - len(tail),
    }


def as_captured_stream(value: Any) -> CapturedStream:
    """``CapturedStream`` как есть; плоская строка — целый необрезанный поток."""
    if isinstance(value, CapturedStream):
        return value
    text = value or ""
    return CapturedStream(head=text, total_chars=len(text))


def exec_channels_json(
    stdout: CapturedStream,
    stderr: CapturedStream,
    preview_edge_chars: int,
) -> dict[str, Any]:
    """``preview`` (если нужен), затем расширенные ``stdout`` / ``stderr``.

    Порядок полей тот же, что у exec-envelope: первая страница
    ``stored_tool_output`` сначала показывает края, не префикс длинного канала.
    """
    channels: dict[str, Any] = {}
    preview: dict[str, Any] = {}
    stdout_preview = stream_preview_json(stdout, preview_edge_chars)
    if stdout_preview is not None:
        preview["stdout"] = stdout_preview
    stderr_preview = stream_preview_json(stderr, preview_edge_chars)
    if stderr_preview is not None:
        preview["stderr"] = stderr_preview
    if preview:
        channels["preview"] = preview
    channels["stdout"] = stream_to_json(stdout)
    channels["stderr"] = stream_to_json(stderr)
    return channels


def build_exec_envelope(
    *,
    tool: str,
    ok: bool,
    exit_code: Optional[int],
    timed_out: bool,
    stdout: CapturedStream,
    stderr: CapturedStream,
    preview_edge_chars: int,
    error: Optional[dict] = None,
    head_fields: Optional[dict] = None,
    exit_code_key: str = "exit_code",
) -> str:
    """Собрать JSON-envelope exec-тулы с front-loaded preview.

    ``head_fields`` — специфика конкретной тулы (``command``/``cwd`` либо
    ``code``), она короткая и идёт до preview. ``exit_code_key`` — историческое
    имя поля кода возврата у конкретной тулы (``exec_python`` — ``return_code``).
    """
    envelope: dict[str, Any] = {"tool": tool, "ok": ok}
    if head_fields:
        envelope.update(head_fields)
    envelope[exit_code_key] = exit_code
    envelope["timed_out"] = timed_out
    if stdout.truncated or stderr.truncated:
        envelope["truncated"] = True
    envelope["error"] = error
    envelope.update(exec_channels_json(stdout, stderr, preview_edge_chars))
    return json.dumps(envelope, ensure_ascii=False)


EMPTY_STREAM = CapturedStream()

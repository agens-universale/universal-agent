"""Wall-clock split of a gc-cli harbor run: waiting on the LLM vs running tools.

Wraps the two entry points of ``processing_responses`` — ``_arequest_backend_canonical``
(every backend call) and ``aapply_tool`` (every tool call) — and appends one ``open``
and one ``end`` record per call to ``timings.jsonl`` in the agent's logs dir. Records
are flushed immediately: when harbor kills a timed-out run, the ``open`` without a
matching ``end`` is the call it was blocked on.

``depth`` is tool nesting, so an LLM call made by a subagent inside a tool has
``depth > 0``; only ``depth == 0`` spans are aggregated, otherwise subagent time is
counted twice. Routing is per thread (the agent loop and its subagents share one
worker thread), so concurrent trials in one process keep separate sinks.
"""
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

_install_lock = threading.Lock()
_installed = False
_sinks: dict[int, "StepTimings"] = {}


def _sink() -> "StepTimings | None":
    return _sinks.get(threading.get_ident())


def _install_once() -> None:
    global _installed
    with _install_lock:
        if _installed:
            return
        from gc_core import processing_responses as pr

        # Оба шва ядра стали корутинами: синхронных ``_request_backend_canonical``
        # и ``apply_tool`` в ``processing_responses`` больше нет — есть шимы над
        # порталом, но агентский цикл ходит мимо них, прямо в ``a``-формы. Обёртка
        # над шимом не увидела бы ни одного вызова и молча показала бы нули.
        orig_llm = pr._arequest_backend_canonical
        orig_tool = pr.aapply_tool

        async def llm_wrapper(messages, state, ctx, **kwargs):
            sink = _sink()
            if sink is None:
                return await orig_llm(messages, state, ctx, **kwargs)
            name = str(getattr(state, "default_model", None) or "llm")
            return await sink.aspan(
                "llm", name, state, lambda: orig_llm(messages, state, ctx, **kwargs)
            )

        async def tool_wrapper(*args, **kwargs):
            sink = _sink()
            if sink is None:
                return await orig_tool(*args, **kwargs)
            name = str(kwargs.get("tool_name") or (args[0] if args else "tool"))
            return await sink.aspan(
                "tool",
                name,
                kwargs.get("state"),
                lambda: orig_tool(*args, **kwargs),
                nested=True,
            )

        pr._arequest_backend_canonical = llm_wrapper
        pr.aapply_tool = tool_wrapper
        _installed = True


class StepTimings:
    """One run's timing sink. ``attach()`` from the thread running the agent loop."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._fh = None
        self._seq = 0
        self._t0 = time.monotonic()
        self.totals = {"llm": 0.0, "tool": 0.0}
        self.counts = {"llm": 0, "tool": 0}
        self.max_sec = {"llm": 0.0, "tool": 0.0}

    def attach(self) -> None:
        _install_once()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._path.open("a", encoding="utf-8")
        with self._lock:
            self._fh = fh
        self._t0 = time.monotonic()
        _sinks[threading.get_ident()] = self

    def close(self) -> None:
        for tid, sink in list(_sinks.items()):
            if sink is self:
                del _sinks[tid]
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()

    def summary(self) -> dict:
        """Aggregates so far — readable while an abandoned worker thread still runs."""
        return {
            "llm_sec": round(self.totals["llm"], 1),
            "tool_sec": round(self.totals["tool"], 1),
            "llm_calls": self.counts["llm"],
            "tool_calls": self.counts["tool"],
            "max_llm_sec": round(self.max_sec["llm"], 1),
            "max_tool_sec": round(self.max_sec["tool"], 1),
        }

    async def aspan(
        self,
        kind: str,
        name: str,
        state: Any,
        call: Callable[[], Any],
        *,
        nested: bool = False,
    ) -> Any:
        """Асинхронный двойник :meth:`span` — оба шва ядра корутинные.

        Учёт и формат записей общие (:meth:`_open_span` / :meth:`_close_span`):
        две копии разошлись бы, и ``timings.jsonl`` синхронного и асинхронного
        прогона перестал бы сравниваться.
        """
        depth, head, started = self._open_span(kind, name, state, nested=nested)
        ok, error = True, None
        try:
            return await call()
        except BaseException as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            self._close_span(
                kind, depth, head, started, ok, error, nested=nested
            )

    def _open_span(
        self, kind: str, name: str, state: Any, *, nested: bool
    ) -> "tuple[int, dict, float]":
        depth = getattr(self._local, "depth", 0)
        with self._lock:
            self._seq += 1
            span_id = self._seq
        head = {
            "id": span_id,
            "kind": kind,
            "name": name,
            "step": int(getattr(state, "steps", 0) or 0),
            "depth": depth,
            "t": round(time.monotonic() - self._t0, 3),
        }
        self._emit({"ev": "open", **head})
        if nested:
            self._local.depth = depth + 1
        return depth, head, time.monotonic()

    def _close_span(
        self,
        kind: str,
        depth: int,
        head: dict,
        started: float,
        ok: bool,
        error: "str | None",
        *,
        nested: bool,
    ) -> None:
        if nested:
            self._local.depth = depth
        elapsed = time.monotonic() - started
        if depth == 0:
            self.totals[kind] += elapsed
            self.counts[kind] += 1
            self.max_sec[kind] = max(self.max_sec[kind], elapsed)
        record = {"ev": "end", **head, "sec": round(elapsed, 3), "ok": ok}
        if error is not None:
            record["error"] = error
        self._emit(record)

    def span(
        self,
        kind: str,
        name: str,
        state: Any,
        call: Callable[[], Any],
        *,
        nested: bool = False,
    ) -> Any:
        depth, head, started = self._open_span(kind, name, state, nested=nested)
        ok, error = True, None
        try:
            return call()
        except BaseException as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            self._close_span(
                kind, depth, head, started, ok, error, nested=nested
            )

    def _emit(self, record: dict) -> None:
        # Читаем дескриптор ПОД ТЕМ ЖЕ локом, под которым пишем. Снаружи была
        # гонка на закрытии: `close()` успевал между чтением `self._fh` и
        # входом в лок, и запись уходила в уже закрытый файл — ValueError из
        # брошенного рабочего потока, который трайл переживает.
        with self._lock:
            fh = self._fh
            if fh is None:
                return
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()


__all__ = ["StepTimings"]

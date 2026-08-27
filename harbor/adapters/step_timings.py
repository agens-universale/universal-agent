"""Wall-clock split of a gc-cli harbor run: waiting on the LLM vs running tools.

Wraps the two entry points of ``processing_responses`` — ``_request_backend_canonical``
(every backend call) and ``apply_tool`` (every tool call) — and appends one ``open``
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
        import processing_responses as pr

        orig_llm = pr._request_backend_canonical
        orig_tool = pr.apply_tool

        def llm_wrapper(messages, state, ctx, **kwargs):
            sink = _sink()
            if sink is None:
                return orig_llm(messages, state, ctx, **kwargs)
            name = str(getattr(state, "default_model", None) or "llm")
            return sink.span(
                "llm", name, state, lambda: orig_llm(messages, state, ctx, **kwargs)
            )

        def tool_wrapper(*args, **kwargs):
            sink = _sink()
            if sink is None:
                return orig_tool(*args, **kwargs)
            name = str(kwargs.get("tool_name") or (args[0] if args else "tool"))
            return sink.span(
                "tool",
                name,
                kwargs.get("state"),
                lambda: orig_tool(*args, **kwargs),
                nested=True,
            )

        pr._request_backend_canonical = llm_wrapper
        pr.apply_tool = tool_wrapper
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
        self._fh = self._path.open("a", encoding="utf-8")
        self._t0 = time.monotonic()
        _sinks[threading.get_ident()] = self

    def close(self) -> None:
        for tid, sink in list(_sinks.items()):
            if sink is self:
                del _sinks[tid]
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

    def span(
        self,
        kind: str,
        name: str,
        state: Any,
        call: Callable[[], Any],
        *,
        nested: bool = False,
    ) -> Any:
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
        started = time.monotonic()
        ok, error = True, None
        if nested:
            self._local.depth = depth + 1
        try:
            return call()
        except BaseException as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
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

    def _emit(self, record: dict) -> None:
        fh = self._fh
        if fh is None:
            return
        with self._lock:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()


__all__ = ["StepTimings"]

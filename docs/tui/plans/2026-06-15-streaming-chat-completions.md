# Streaming `chat_completions` → live TUI output — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream `chat_completions` SSE so the TUI shows live in-progress model output (compact `<think>` + growing answer), replaced by the canonical thread row on completion — without changing agent-loop behavior.

**Architecture:** Two layers. Layer 1 (transport): `request_backend_chat_completions` gains an optional `on_delta` callback; when present and `backend.stream` is on, it reads SSE with `stream=True` and **reassembles the exact same response object** as the non-stream path. Layer 2 (presentation, TUI-only): a thread-safe `StreamSink` is fed by `on_delta` and drained by the existing 250 ms `_poll_active` tick to render an ephemeral block, cleared when the canonical assistant row arrives. `State` (domain SoT) is untouched; the thread store stays the only source of persisted truth.

**Tech Stack:** Python ≥3.10, `requests` (streaming), pytest, Textual (TUI).

**Spec:** `docs/tui/specs/2026-06-15-streaming-chat-completions-design.md`

---

## File Structure

- **Create** `src/utils/chat_completions_stream.py` — SSE line iteration + `assemble_streamed_completion` (pure, testable; reassembles `r.json()`-shape, emits `on_delta`).
- **Modify** `src/utils/connect_backend.py` — `request_backend_chat_completions(..., on_delta=None)`: stream branch vs current blocking branch.
- **Modify** `src/request_backend_policy.py` — thread `on_delta` through `request_chat_completions_with_policy` → `_send_once`.
- **Modify** `src/config_modules/backend.py` — add `stream: bool = True`.
- **Modify** `src/config.py` — surface `"stream"` into `config_dict`.
- **Modify** `src/run_context.py` — `on_stream_delta` param + `RunContext` field.
- **Modify** `src/processing_responses.py` — pass `on_delta` (gated by `cfg["stream"]`) into the policy call.
- **Create** `src/tui/services/stream_sink.py` — `StreamSink` (thread-safe buffers) + `format_stream_block` (pure formatter, inline `<think>` handling).
- **Modify** `src/tui/app.py` — create `self._stream_sink`, pass `on_stream_delta=self._stream_sink.push`.
- **Modify** `src/tui/screens/main.py` — render/update ephemeral `StreamBlock` from sink in `_poll_active`; reset sink when a canonical assistant row is rendered or run ends.
- **Tests** under `tests/` and `tests/tui/`.

> **Layering note (CLAUDE.md):** `chat_completions_stream.py` and `connect_backend.py` are transport (no business logic). `stream_sink.py` is presentation. `on_stream_delta` lives on `RunContext` (per-run presentation callback, like the existing `on_progress`) — **never** on `State`.

---

## Task 0: Capture a real SSE sample (real-payload-first)

**Files:** none (produces a reference fixture for Task 2).

- [ ] **Step 1: Capture a real streamed completion from the actual backend**

Run (substitute the SGLang/gigacode endpoint + token actually in use):

```bash
curl -N -sS http://46.243.227.147:15000/v1/chat/completions \
  -H "Authorization: Bearer sk_sglang_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gigacode_agent_v1_rc","stream":true,"max_tokens":128,
       "messages":[{"role":"user","content":"привет, ответь одним словом и вызови любой инструмент если есть"}],
       "tools":[{"type":"function","function":{"name":"ping","parameters":{"type":"object","properties":{}}}}],
       "tool_choice":"auto"}' | tee tests/fixtures/sglang_stream_sample.txt
```

Expected: a sequence of `data: {...}` lines ending with `data: [DONE]`. Note whether reasoning arrives as `delta.reasoning_content`, inline `<think>…</think>` in `delta.content`, and how `delta.tool_calls[].function.arguments` are fragmented, and where `usage`/`finish_reason` appear.

- [ ] **Step 2: Record findings inline in Task 2's test**

If the real format deviates from the OpenAI-compatible shape used in Task 2's tests (e.g., a different reasoning field name), adjust the fixture strings in Task 2 to match the captured sample before implementing. Commit the fixture:

```bash
git add tests/fixtures/sglang_stream_sample.txt
git commit -m "test(stream): capture real SGLang chat/completions SSE sample"
```

> If the endpoint is unreachable at implementation time, proceed with the OpenAI-compatible samples in Task 2 (SGLang follows that wire format) and revisit when access is available.

---

## Task 1: Config key `backend.stream`

**Files:**
- Modify: `src/config_modules/backend.py:33-45`
- Modify: `src/config.py:140-150`
- Test: `tests/test_backend_stream_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend_stream_config.py
from config_modules.backend import BackendConfig


def test_backend_stream_defaults_true():
    assert BackendConfig().stream is True


def test_backend_stream_can_be_disabled():
    assert BackendConfig(stream=False).stream is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend_stream_config.py -v`
Expected: FAIL — `BackendConfig` has no field `stream` (extra=forbid would also reject the kwarg).

- [ ] **Step 3: Add the field**

In `src/config_modules/backend.py`, inside `class BackendConfig`, alongside the other fields (e.g. after `use_retry: bool = False`):

```python
    stream: bool = True
```

- [ ] **Step 4: Surface it into config_dict**

In `src/config.py`, in the `config_dict` literal next to the other `backend.*` keys (after `"use_retry": app_cfg.backend.use_retry,`):

```python
        "stream": app_cfg.backend.stream,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_backend_stream_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/config_modules/backend.py src/config.py tests/test_backend_stream_config.py
git commit -m "feat(backend): add backend.stream config flag (default on)"
```

---

## Task 2: SSE reassembly — `assemble_streamed_completion`

**Files:**
- Create: `src/utils/chat_completions_stream.py`
- Test: `tests/test_chat_completions_stream.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_completions_stream.py
from utils.chat_completions_stream import (
    iter_sse_data_lines,
    assemble_streamed_completion,
)

# Realistic OpenAI-compatible stream: reasoning_content, content, a fragmented
# tool call, finish_reason, and usage in the final chunk. (Cross-check against
# tests/fixtures/sglang_stream_sample.txt from Task 0.)
_LINES = [
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"ана"}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"reasoning_content":"лизирую"}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"Это "}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"проект."}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"t1","type":"function","function":{"name":"ping","arguments":"{\\"a\\":"}}]}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"completion_tokens":42}}',
    'data: [DONE]',
    '',
]


def test_iter_sse_data_lines_skips_done_and_blanks():
    payloads = list(iter_sse_data_lines(_LINES))
    assert payloads[0].startswith('{"id":"c1"')
    assert all(p != "[DONE]" and p != "" for p in payloads)
    assert len(payloads) == 7


def test_assemble_reassembles_message_content_and_tool_calls():
    deltas = []
    result = assemble_streamed_completion(
        iter_sse_data_lines(_LINES),
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )
    msg = result["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Это проект."
    assert msg["reasoning_content"] == "анализирую"
    assert msg["tool_calls"] == [
        {"id": "t1", "type": "function",
         "function": {"name": "ping", "arguments": '{"a":1}'}}
    ]
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["usage"] == {"completion_tokens": 42}
    # on_delta surfaced think + answer chunks in order
    assert ("think", "ана") in deltas
    assert ("answer", "Это ") in deltas
    assert [k for k, _ in deltas if k == "answer"] == ["Это ", "проект."]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chat_completions_stream.py -v`
Expected: FAIL — module `utils.chat_completions_stream` does not exist.

- [ ] **Step 3: Implement the module**

```python
# src/utils/chat_completions_stream.py
"""SSE parsing + reassembly for streamed chat/completions.

Pure transport helpers: reassemble an OpenAI-compatible chat.completion object
from streamed SSE chunks, byte-compatible with the non-stream `r.json()` shape,
while emitting (kind, text) deltas for live display. No business logic.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

OnDelta = Callable[[str, str], None]


def iter_sse_data_lines(raw_lines: Iterable) -> Iterable[str]:
    """Yield JSON payload strings from SSE `data:` lines; stop at `[DONE]`."""
    for line in raw_lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", errors="replace")
        line = (line or "").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        yield data


def assemble_streamed_completion(
    data_payloads: Iterable[str],
    on_delta: Optional[OnDelta] = None,
) -> Dict[str, Any]:
    """Reassemble a chat.completion object from SSE chunk JSON payloads.

    Emits on_delta("think"|"answer", text) per chunk: reasoning_content -> think,
    content -> answer. Inline <think> handling is left to the display layer (the
    content is preserved verbatim here).
    """
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    role = "assistant"
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    resp_id: Optional[str] = None

    for payload in data_payloads:
        try:
            chunk = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        if model is None and chunk.get("model"):
            model = chunk["model"]
        if resp_id is None and chunk.get("id"):
            resp_id = chunk["id"]

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        ch0 = choices[0]
        if not isinstance(ch0, dict):
            continue
        if ch0.get("finish_reason"):
            finish_reason = ch0["finish_reason"]
        delta = ch0.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        if delta.get("role"):
            role = delta["role"]

        rc = delta.get("reasoning_content")
        if isinstance(rc, str) and rc:
            reasoning_parts.append(rc)
            if on_delta is not None:
                on_delta("think", rc)

        c = delta.get("content")
        if isinstance(c, str) and c:
            content_parts.append(c)
            if on_delta is not None:
                on_delta("answer", c)

        for tc in (delta.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(
                idx,
                {"id": None, "type": "function",
                 "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                slot["id"] = tc["id"]
            if tc.get("type"):
                slot["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    message: Dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    result: Dict[str, Any] = {
        "id": resp_id,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        result["usage"] = usage
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chat_completions_stream.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/utils/chat_completions_stream.py tests/test_chat_completions_stream.py
git commit -m "feat(stream): SSE reassembly for chat/completions"
```

---

## Task 3: Streaming branch in `request_backend_chat_completions`

**Files:**
- Modify: `src/utils/connect_backend.py:136-223`
- Test: `tests/test_connect_backend_streaming.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connect_backend_streaming.py
import json
import types
import requests
from utils import connect_backend


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_lines(self):
        for ln in self._lines:
            yield ln.encode("utf-8")


def test_streaming_path_used_when_on_delta_given(monkeypatch):
    lines = [
        'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}',
        'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":1}}',
        'data: [DONE]',
    ]
    captured = {}
    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        captured["stream"] = stream
        return _FakeStreamResponse(lines)
    monkeypatch.setattr(connect_backend.requests, "post", fake_post)

    deltas = []
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
        on_delta=lambda k, t: deltas.append((k, t)),
    )
    assert captured["stream"] is True
    assert res["choices"][0]["message"]["content"] == "hi"
    assert res["usage"] == {"completion_tokens": 1}
    assert ("answer", "hi") in deltas


def test_blocking_path_when_no_on_delta(monkeypatch):
    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        assert stream is False
        r = types.SimpleNamespace()
        r.raise_for_status = lambda: None
        r.json = lambda: {"choices": [{"message": {"content": "blk"}}]}
        return r
    monkeypatch.setattr(connect_backend.requests, "post", fake_post)
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
    )
    assert res["choices"][0]["message"]["content"] == "blk"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_connect_backend_streaming.py -v`
Expected: FAIL — `request_backend_chat_completions` does not accept `on_delta`.

- [ ] **Step 3: Add the streaming branch**

In `src/utils/connect_backend.py`, add the import near the top (after the existing `from utils.retry import retry_with_backoff`):

```python
from utils.chat_completions_stream import iter_sse_data_lines, assemble_streamed_completion
```

Change the signature of `request_backend_chat_completions` to add `on_delta=None` (keep the `@retry_with_backoff` decorator):

```python
def request_backend_chat_completions(
    data: Dict,
    headers: Dict,
    url: str,
    verify: bool = True,
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
    on_delta=None,
):
```

At the **start of the function body** (before the existing `if logger is not None:` debug block), add the streaming branch:

```python
    if on_delta is not None:
        stream_data = dict(data)
        stream_data["stream"] = True
        with requests.post(
            url, json=stream_data, headers=headers, stream=True, verify=verify
        ) as r:
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError:
                # Preserve error-body diagnostics (parity with blocking path).
                err_text = None
                try:
                    err_text = r.text
                except Exception:
                    err_text = None
                if logger is not None:
                    logger.error(
                        "Backend chat/completions (stream) HTTP error status=%s url=%s body=%s",
                        getattr(r, "status_code", None), url, err_text,
                    )
                print(
                    "=== backend chat/completions (stream) error ===\n"
                    f"url={url}\nstatus={getattr(r, 'status_code', None)}\n"
                    f"response_body={(err_text or '')[:4000]}\n"
                )
                raise
            return assemble_streamed_completion(
                iter_sse_data_lines(r.iter_lines()), on_delta=on_delta
            )
```

Leave the rest of the function (the blocking `r = requests.post(...)` path) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_connect_backend_streaming.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/utils/connect_backend.py tests/test_connect_backend_streaming.py
git commit -m "feat(stream): streaming branch in request_backend_chat_completions"
```

---

## Task 4: Thread `on_delta` through the request policy

**Files:**
- Modify: `src/request_backend_policy.py:36-68`
- Test: `tests/test_request_policy_on_delta.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_request_policy_on_delta.py
import request_backend_policy as policy


def test_policy_forwards_on_delta(monkeypatch):
    seen = {}
    def fake_req(payload, headers, *, url, verify, use_retry, logger, on_delta=None):
        seen["on_delta"] = on_delta
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(policy, "request_backend_chat_completions", fake_req)

    sentinel = lambda k, t: None
    res, payload = policy.request_chat_completions_with_policy(
        payload_factory=lambda mt: {"model": "m", "max_tokens": mt, "messages": []},
        initial_max_tokens=100, min_response_tokens=10,
        headers={}, url="http://x", verify=True, use_retry=False,
        on_delta=sentinel,
    )
    assert seen["on_delta"] is sentinel
    assert res["choices"][0]["message"]["content"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_request_policy_on_delta.py -v`
Expected: FAIL — `request_chat_completions_with_policy` has no `on_delta` param.

- [ ] **Step 3: Add and forward the param**

In `src/request_backend_policy.py`, add `on_delta=None` to the `request_chat_completions_with_policy` signature (after `logger`):

```python
    logger: logging.Logger | None = None,
    on_delta=None,
```

Then in the inner `_send_once`, forward it to the transport call:

```python
        response = request_backend_chat_completions(
            payload,
            headers,
            url=url,
            verify=verify,
            use_retry=transport_use_retry,
            logger=logger,
            on_delta=on_delta,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_request_policy_on_delta.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/request_backend_policy.py tests/test_request_policy_on_delta.py
git commit -m "feat(stream): forward on_delta through request policy"
```

---

## Task 5: `RunContext.on_stream_delta`

**Files:**
- Modify: `src/run_context.py:20-99`
- Test: `tests/test_run_context_on_stream_delta.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_context_on_stream_delta.py
from run_context import create_run_context


def test_on_stream_delta_defaults_none(minimal_config_dict):
    ctx = create_run_context(minimal_config_dict)
    assert ctx.on_stream_delta is None


def test_on_stream_delta_is_stored(minimal_config_dict):
    cb = lambda k, t: None
    ctx = create_run_context(minimal_config_dict, on_stream_delta=cb)
    assert ctx.on_stream_delta is cb
```

> `minimal_config_dict` — reuse an existing config-dict fixture if `tests/conftest.py` defines one; otherwise build via `from config import load_config` against `demo_config.yml`. Check `tests/conftest.py` for the established helper before adding a new fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_run_context_on_stream_delta.py -v`
Expected: FAIL — `RunContext` has no `on_stream_delta`.

- [ ] **Step 3: Add field + param**

In `src/run_context.py`, add the field to the `RunContext` dataclass (after `project_gc_dir`):

```python
    on_stream_delta: Optional[Callable[[str, str], None]] = None
```

Ensure `Callable` is imported (`from typing import ... Callable`). Add the param to `create_run_context` (after `on_progress=None,`):

```python
    on_stream_delta=None,
```

And pass it into the `RunContext(...)` constructor:

```python
        on_stream_delta=on_stream_delta,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_run_context_on_stream_delta.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/run_context.py tests/test_run_context_on_stream_delta.py
git commit -m "feat(stream): RunContext.on_stream_delta presentation callback"
```

---

## Task 6: Wire `on_delta` from `processing_responses` (gated by `backend.stream`)

**Files:**
- Modify: `src/processing_responses.py:213-222`
- Test: `tests/test_processing_responses_stream_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_processing_responses_stream_wiring.py
from types import SimpleNamespace
import processing_responses as pr


def _patch_policy(monkeypatch):
    seen = {}
    def fake_policy(*, payload_factory, initial_max_tokens, min_response_tokens,
                    headers, url, verify, use_retry, logger, on_delta=None):
        seen["on_delta"] = on_delta
        return ({"choices": [{"message": {"role": "assistant", "content": "x"}}]},
                {"max_tokens": initial_max_tokens})
    monkeypatch.setattr(pr, "request_chat_completions_with_policy", fake_policy)
    return seen


def test_on_delta_passed_when_stream_on(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=True)
    cb = lambda k, t: None
    ctx.on_stream_delta = cb
    pr._request_backend_canonical(messages, state, ctx)  # match real signature
    assert seen["on_delta"] is cb


def test_on_delta_none_when_stream_off(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=False)
    ctx.on_stream_delta = lambda k, t: None
    pr._request_backend_canonical(messages, state, ctx)
    assert seen["on_delta"] is None
```

> The chat_completions branch lives in `_request_backend_canonical(messages, state, ctx, ...)` (def at `src/processing_responses.py:144`; branch with `url = cfg["completions_url"]` at line 184). Open it to confirm the exact positional/keyword params and the `set_previous_response_id` default, then have the `chat_state_ctx` fixture build a minimal `ctx` (`config_dict={"completions_url": "http://x", "stream": <bool>, "chat_completions_max_tokens": 256, ...}`, `on_stream_delta`, `logger`), a minimal `state` (with `.messages`, `.tools`, `.default_model`), and the `messages` arg. Adjust the call to the real signature.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_processing_responses_stream_wiring.py -v`
Expected: FAIL — policy is called without `on_delta` (it's `None` in the stream-on case).

- [ ] **Step 3: Pass on_delta gated by config**

In `src/processing_responses.py`, just before the `request_chat_completions_with_policy(...)` call (line 213), compute the gated callback:

```python
    _stream_on = bool(cfg.get("stream", True))
    _on_delta = getattr(ctx, "on_stream_delta", None) if _stream_on else None
```

Add `on_delta=_on_delta,` to the `request_chat_completions_with_policy(...)` call arguments (after `logger=getattr(ctx, "logger", None),`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_processing_responses_stream_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/processing_responses.py tests/test_processing_responses_stream_wiring.py
git commit -m "feat(stream): wire on_delta from processing_responses (gated by backend.stream)"
```

---

## Task 7: `StreamSink` + `format_stream_block`

**Files:**
- Create: `src/tui/services/stream_sink.py`
- Test: `tests/tui/test_stream_sink.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_stream_sink.py
from tui.services.stream_sink import StreamSink, format_stream_block


def test_sink_accumulates_and_resets():
    s = StreamSink()
    s.push("think", "ана")
    s.push("answer", "Это ")
    s.push("answer", "проект.")
    think, answer = s.snapshot()
    assert think == "ана"
    assert answer == "Это проект."
    assert s.has_content() is True
    s.reset()
    assert s.snapshot() == ("", "")
    assert s.has_content() is False


def test_format_strips_inline_think_to_tail():
    # Inline <think> in the answer buffer is surfaced as compact think-tail,
    # not as answer text; the answer shows only post-think content.
    block = format_stream_block(
        think="", answer="<think>анализирую структуру\nсмотрю agents/</think>Это проект."
    )
    assert "Это проект." in block
    assert "анализирую структуру" not in block  # full think not dumped
    assert "смотрю agents/" in block             # only the last think line (tail)


def test_format_reasoning_channel_as_think_tail():
    block = format_stream_block(think="строю план\nчитаю README", answer="")
    assert "читаю README" in block      # tail of reasoning
    assert "строю план" not in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/tui/test_stream_sink.py -v` (via `uv run pytest` per the TUI env note)
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement StreamSink + formatter**

```python
# src/tui/services/stream_sink.py
"""StreamSink — thread-safe buffer for live chat/completions deltas (TUI-only).

Fed by RunContext.on_stream_delta from the agent worker thread; drained by the
TUI poll tick. `format_stream_block` renders a compact in-progress view: the
answer text plus a single tail line of reasoning (full <think> is never dumped).
Presentation layer — no business logic, not a substitute for the thread store.
"""
from __future__ import annotations

import re
import threading

_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL)


class StreamSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._think = ""
        self._answer = ""

    def push(self, kind: str, text: str) -> None:
        with self._lock:
            if kind == "think":
                self._think += text
            else:
                self._answer += text

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._think, self._answer

    def has_content(self) -> bool:
        with self._lock:
            return bool(self._think or self._answer)

    def reset(self) -> None:
        with self._lock:
            self._think = ""
            self._answer = ""


def _tail_line(s: str) -> str:
    lines = [ln for ln in s.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def format_stream_block(*, think: str, answer: str) -> str:
    """Compact in-progress view. Inline <think> spans in `answer` are stripped
    out (their tail shown as reasoning), leaving only the real answer text."""
    inline_think = " ".join(m.group(1) for m in _THINK_RE.finditer(answer))
    answer_clean = _THINK_RE.sub("", answer).strip()
    think_tail = _tail_line(inline_think) or _tail_line(think)

    lines = []
    if think_tail:
        lines.append(f"⠿ думаю… {think_tail}")
    if answer_clean:
        lines.append(answer_clean)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tui/test_stream_sink.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/stream_sink.py tests/tui/test_stream_sink.py
git commit -m "feat(stream): StreamSink + compact in-progress formatter"
```

---

## Task 8: App wiring — create sink, pass `on_stream_delta`

**Files:**
- Modify: `src/tui/app.py:80-99` (`_build_real_executor`) and `App.__init__`
- Test: `tests/tui/test_app_stream_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_app_stream_wiring.py
from tui.app import TuiApp


def test_app_has_stream_sink():
    app = TuiApp(config={})  # match the real TuiApp.__init__ signature
    assert app._stream_sink is not None
    # push/snapshot smoke
    app._stream_sink.push("answer", "x")
    assert app._stream_sink.snapshot()[1] == "x"
```

> App class is `TuiApp` (`src/tui/app.py:15`). Open its `__init__` to match the real constructor kwargs (config/threads_dir/system/executor_factory etc.); pass only what's required. Keep this test minimal — full wiring is asserted via the TUI test in Task 9.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tui/test_app_stream_wiring.py -v`
Expected: FAIL — `app._stream_sink` does not exist.

- [ ] **Step 3: Create the sink and pass the callback**

In `src/tui/app.py`, import at top:

```python
from tui.services.stream_sink import StreamSink
```

In `App.__init__`, alongside other instance attrs (e.g. near `self._mcp_progress = None` / wherever per-app state is initialized):

```python
        self._stream_sink = StreamSink()
```

In `_build_real_executor`, update the `create_run_context` call (line 94) to pass the callback:

```python
        ctx = create_run_context(
            cfg, auth_connector=auth, on_progress=progress.update,
            on_stream_delta=self._stream_sink.push,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tui/test_app_stream_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tui/app.py tests/tui/test_app_stream_wiring.py
git commit -m "feat(stream): app creates StreamSink and wires on_stream_delta"
```

---

## Task 9: Render the ephemeral StreamBlock in the TUI

**Files:**
- Modify: `src/tui/screens/main.py` (`_poll_active` render section ~1098-1142; row-render path that mounts assistant rows)
- Test: `tests/tui/test_stream_block_render.py`

> **Read first (no code change):** `src/tui/widgets/thinking_indicator.py` (mount/update pattern via `Static` + `set_interval`/`refresh`) and the `_poll_active` body in `src/tui/screens/main.py` (how rows are mounted into the trace scroll, `_show_thinking_indicator`/`_hide_thinking_indicator`, and where canonical assistant rows are rendered). The `StreamBlock` mirrors `ThinkingIndicator`'s `Static` mount pattern and lives just below the thinking indicator in the trace scroll.

- [ ] **Step 1: Write the failing test (logic-level, headless)**

```python
# tests/tui/test_stream_block_render.py
# Verifies the screen's pure decision helper for the ephemeral block:
# given a live sink with content and an active run, the block text is produced;
# once a canonical assistant row for the step is rendered, the sink is reset.
from tui.services.stream_sink import StreamSink, format_stream_block


def test_block_text_from_sink():
    s = StreamSink()
    s.push("think", "читаю README")
    s.push("answer", "Это проект.")
    think, answer = s.snapshot()
    assert format_stream_block(think=think, answer=answer) == (
        "⠿ думаю… читаю README\nЭто проект."
    )


def test_sink_reset_clears_block():
    s = StreamSink()
    s.push("answer", "partial")
    s.reset()
    think, answer = s.snapshot()
    assert format_stream_block(think=think, answer=answer) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: PASS for the pure helpers if Task 7 is done — these guard the contract the screen relies on. (If they already pass, proceed; they exist to lock the format the render uses.)

- [ ] **Step 3: Add the StreamBlock widget**

Create `src/tui/widgets/stream_block.py`:

```python
# src/tui/widgets/stream_block.py
"""Ephemeral in-progress streaming block (TUI). Shows live model output for the
current step; removed when the canonical thread row arrives or the run ends."""
from __future__ import annotations

from textual.widgets import Static


class StreamBlock(Static):
    DEFAULT_CSS = ""  # styled via .stream-block in styles.tcss

    def __init__(self) -> None:
        super().__init__("", classes="stream-block", markup=False)

    def set_text(self, text: str) -> None:
        if self.is_mounted:
            self.update(text)
```

Add a `.stream-block` style in `src/tui/styles.tcss` (mirror `.thinking-indicator`'s color/padding; dim accent).

- [ ] **Step 4: Render/clear in `_poll_active`**

In `src/tui/screens/main.py`, add an instance attr `self._stream_block = None` where other trace widgets are initialized. In `_poll_active`, in the `if is_running and not ask_human_active:` block (after the thinking-indicator logic), drain the sink:

```python
            sink = getattr(self.app, "_stream_sink", None)
            if sink is not None and sink.has_content():
                from tui.services.stream_sink import format_stream_block
                from tui.widgets.stream_block import StreamBlock
                think, answer = sink.snapshot()
                text = format_stream_block(think=think, answer=answer)
                if self._stream_block is None:
                    self._stream_block = StreamBlock()
                    <trace_scroll>.mount(self._stream_block)  # same container ThinkingIndicator mounts into
                self._stream_block.set_text(text)
```

In the path that renders a **canonical assistant row** from the thread (and in the run-ended / `[stopped]` branch), clear the block and reset the sink:

```python
            if self._stream_block is not None:
                self._stream_block.remove()
                self._stream_block = None
            sink = getattr(self.app, "_stream_sink", None)
            if sink is not None:
                sink.reset()
```

> Replace `<trace_scroll>` with the actual scroll container used by `_show_thinking_indicator` (find it by reading that method). Place the "render canonical row → clear block" hook at the same spot where new assistant `message` rows are mounted from polled thread records.

- [ ] **Step 5: Add a TUI integration test (snapshot or state)**

```python
# append to tests/tui/test_stream_block_render.py
import pytest


@pytest.mark.asyncio
async def test_stream_block_appears_and_clears(<tui_harness_fixture>):
    """Drive the screen with a fake runner: push deltas to app._stream_sink,
    tick _poll_active → StreamBlock mounted with text; then emit a canonical
    assistant row → StreamBlock removed and sink reset."""
    # Build using the established TUI test harness in tests/tui/ (see neighboring
    # tests, e.g. test_main_screen_emptystate.py, for the app pilot pattern).
    ...
```

> Model this on the existing TUI harness used by neighboring `tests/tui/test_*` (Textual `App.run_test()` pilot). Assert: after pushing deltas + a poll tick, a `StreamBlock` exists with expected text; after a canonical assistant row renders, no `StreamBlock` remains and `app._stream_sink.snapshot() == ("", "")`. Per the TUI flakiness note, run this file in isolation.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/tui/widgets/stream_block.py src/tui/styles.tcss src/tui/screens/main.py tests/tui/test_stream_block_render.py
git commit -m "feat(stream): ephemeral StreamBlock live render in TUI"
```

---

## Task 10: Manual end-to-end verification

**Files:** none (manual).

- [ ] **Step 1: Run the TUI against the streaming endpoint**

Configure `config.yml` with a `chat_completions` profile that streams (`backend.stream: true`, an endpoint that supports SSE), launch the TUI, send a prompt to a slow/reasoning model.
Expected: live `⠿ думаю…` tail + growing answer text appears during generation; on completion the ephemeral block is replaced by the canonical rendered message; tool-call steps still work.

- [ ] **Step 2: Verify the kill-switch**

Set `backend.stream: false`, repeat.
Expected: behaves exactly as before (blocking; no ephemeral block), no regressions.

- [ ] **Step 3: Targeted regression run**

Run: `uv run pytest tests/test_chat_completions_stream.py tests/test_connect_backend_streaming.py tests/test_request_policy_on_delta.py tests/test_backend_stream_config.py tests/tui/test_stream_sink.py tests/tui/test_stream_block_render.py -v`
Expected: all PASS. Also run `pytest tests/test_summary_pipeline.py tests/test_backend_mode_switch.py -v` to confirm no chat_completions-path regression.

---

## Self-Review notes (for the implementer)

- **Spec coverage:** Tasks map to spec §6 (1→config, 2/3→transport, 4→policy, 5/6→wiring, 7/8/9→presentation), §7 edge cases (HTTP-error body preserved in Task 3; stop/retry reset in Task 9/Task 7 `reset`), §10 testing (Task 0 real sample; unit/regression/TUI throughout).
- **Placeholders that MUST be resolved by reading code (not left vague):** real app class/constructor (Task 8), `processing_responses` entrypoint name + `chat_state_ctx` fixture (Task 6), the trace scroll container + canonical-row mount point (Task 9). These are explicit "read this file, use the real name" steps, not invented APIs.
- **Type consistency:** `on_delta(kind, text)` and `StreamSink.push(kind, text)` share the same `("think"|"answer", str)` contract end to end; `assemble_streamed_completion` returns the `{"choices":[{"message":...,"finish_reason":...}], "usage":...}` shape consumed unchanged at `processing_responses.py:223+`.

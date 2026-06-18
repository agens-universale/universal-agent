# Reasoning Indicator (no content, Claude-style, liveness proof) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show reasoning as an indicator only (no content rows): the `ThinkingIndicator` gains a Claude-style tail `(Nс · ↓ N токенов · размышление / обдумал за Nс)` driven by real stream deltas, and reasoning content rows are suppressed.

**Architecture:** Extends the streaming branch. `StreamSink` tracks a self-counted progress counter + reasoning start/end timestamps from `on_delta` deltas. `ThinkingIndicator` reads the sink each render and appends the tail. Reasoning trace rows are suppressed at the mapper. The live answer keeps streaming (StreamBlock shows answer only). No new widget.

**Tech Stack:** Python ≥3.10, pytest, Textual (TUI).

**Spec:** `docs/tui/specs/2026-06-16-reasoning-indicator-design.md`
**Branch:** `feat/tui-stream-chat-completions` (continue here — надстройка над стримингом).

---

## File Structure

- **Modify** `src/tui/services/stream_sink.py` — `StreamSink`: add `_chars`, `_reasoning_started_at`, `_reasoning_ended_at`, injectable `clock`; new `progress_units()` and `reasoning_state()`; `reset`/`push("reset")` clear them. `format_stream_block` → answer only (drop think tail); remove now-orphan `_tail_line`.
- **Modify** `src/tui/widgets/thinking_indicator.py` — elapsed `s → с`; optional `sink` param; in the "thinking" state append `· ↓ N токенов` + reasoning state tag; helper `_fmt_units`.
- **Modify** `src/tui/screens/main.py` — construct `ThinkingIndicator(sink=app._stream_sink)`; `format_stream_block(answer=…)`; remove dead think-row mount.
- **Modify** `src/tui/services/trace_mapper.py` — reasoning/think → `None` (suppress row).
- **Modify** `src/tui/services/trace_render.py` — remove now-dead think render branch.
- **Tests** under `tests/tui/`.

> **Out of scope (leave untouched):** `src/tui/services/export.py` reasoning handling (export is an explicit user action; reasoning in exported trace is fine). `src/tui/widgets/tool_call_row.py` already uses `с` — not touched.

---

## Task 1: StreamSink — progress counter + reasoning timing

**Files:**
- Modify: `src/tui/services/stream_sink.py`
- Test: `tests/tui/test_stream_sink.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/tui/test_stream_sink.py`)

```python
def test_sink_progress_units_counts_all_chars():
    s = StreamSink()
    s.push("think", "abcd")     # 4 chars
    s.push("answer", "efgh")    # +4 chars = 8
    assert s.progress_units() == 2          # 8 // 4


def test_reasoning_state_none_when_no_think():
    s = StreamSink()
    s.push("answer", "hi")
    assert s.reasoning_state() == (None, None)


def test_reasoning_state_thinking_then_done():
    clk = {"t": 100.0}
    s = StreamSink(clock=lambda: clk["t"])
    s.push("think", "план")
    assert s.reasoning_state() == ("thinking", None)
    clk["t"] = 106.0
    s.push("answer", "ответ")
    state, dur = s.reasoning_state()
    assert state == "done"
    assert round(dur) == 6


def test_reset_clears_progress_and_reasoning():
    clk = {"t": 0.0}
    s = StreamSink(clock=lambda: clk["t"])
    s.push("think", "xxxx")
    s.push("reset", "")
    assert s.progress_units() == 0
    assert s.reasoning_state() == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tui/test_stream_sink.py -v` (fallback `python -m pytest`)
Expected: FAIL — `StreamSink` has no `clock`/`progress_units`/`reasoning_state`.

- [ ] **Step 3: Implement** — replace the `StreamSink` class body in `src/tui/services/stream_sink.py` (keep the module docstring/imports; add `import time`):

```python
import time
import threading


class StreamSink:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._think = ""
        self._answer = ""
        self._chars = 0
        self._reasoning_started_at = None  # float | None
        self._reasoning_ended_at = None    # float | None

    def push(self, kind: str, text: str) -> None:
        with self._lock:
            if kind == "reset":
                self._think = ""
                self._answer = ""
                self._chars = 0
                self._reasoning_started_at = None
                self._reasoning_ended_at = None
                return
            if kind == "think":
                self._think += text
                self._chars += len(text)
                if self._reasoning_started_at is None:
                    self._reasoning_started_at = self._clock()
            else:  # "answer" (default)
                self._answer += text
                self._chars += len(text)
                if (
                    self._reasoning_started_at is not None
                    and self._reasoning_ended_at is None
                ):
                    self._reasoning_ended_at = self._clock()

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._think, self._answer

    def has_content(self) -> bool:
        with self._lock:
            return bool(self._think or self._answer)

    def progress_units(self) -> int:
        """~tokens received from the stream (chars // 4). Liveness counter —
        self-measured, independent of backend `usage`."""
        with self._lock:
            return self._chars // 4

    def reasoning_state(self):
        """(None, None) | ("thinking", None) | ("done", duration_sec)."""
        with self._lock:
            if self._reasoning_started_at is None:
                return (None, None)
            if self._reasoning_ended_at is None:
                return ("thinking", None)
            return ("done", self._reasoning_ended_at - self._reasoning_started_at)

    def reset(self) -> None:
        with self._lock:
            self._think = ""
            self._answer = ""
            self._chars = 0
            self._reasoning_started_at = None
            self._reasoning_ended_at = None
```

(Remove the old `import re` if `_THINK_RE` is no longer referenced after Task 2; if still referenced this task, leave it — Task 2 handles it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tui/test_stream_sink.py -v`
Expected: PASS (new tests; pre-existing sink tests may break on `format_stream_block` — fixed in Task 2. If they fail here, proceed to Task 2 which updates them, then re-run.)

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/stream_sink.py tests/tui/test_stream_sink.py
git commit -m "feat(stream): StreamSink progress counter + reasoning timing"
```

---

## Task 2: `format_stream_block` → answer only

**Files:**
- Modify: `src/tui/services/stream_sink.py`
- Modify: `src/tui/screens/main.py` (the StreamBlock render call)
- Test: `tests/tui/test_stream_sink.py` (update existing format tests)

- [ ] **Step 1: Update the failing tests** — in `tests/tui/test_stream_sink.py`, REPLACE the existing `format_stream_block` tests (`test_format_strips_inline_think_to_tail`, `test_format_reasoning_channel_as_think_tail`, `test_format_empty_returns_empty`, `test_format_unclosed_think_still_streaming`, and the `format_stream_block(...)` call inside `test_sink_reset_clears_block`) with these:

```python
def test_format_shows_answer_only():
    # Reasoning now goes to the indicator; the block shows ONLY the answer.
    assert format_stream_block(answer="Это проект.") == "Это проект."


def test_format_strips_inline_think_from_answer():
    block = format_stream_block(
        answer="<think>анализирую\nсмотрю agents/</think>Это проект."
    )
    assert block == "Это проект."
    assert "<think>" not in block
    assert "анализирую" not in block


def test_format_empty_returns_empty():
    assert format_stream_block(answer="") == ""


def test_format_unclosed_think_stripped():
    # Unclosed <think> still streaming → stripped, empty answer remainder.
    assert format_stream_block(answer="<think>анализирую\nсмотрю agents/") == ""
```

Also update `test_sink_reset_clears_block` to call `format_stream_block(answer=answer)` (drop the `think=` kwarg).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tui/test_stream_sink.py -v`
Expected: FAIL — `format_stream_block` still requires `think=` and renders the «думаю…» tail.

- [ ] **Step 3: Implement** — in `src/tui/services/stream_sink.py`, replace `format_stream_block` and remove the now-orphan `_tail_line` (keep `_THINK_RE`):

```python
def format_stream_block(*, answer: str) -> str:
    """In-progress view for the StreamBlock: ONLY the answer text.
    Reasoning is surfaced via the ThinkingIndicator, not here. Inline
    <think>...</think> spans (incl. an unclosed one mid-stream) are stripped."""
    return _THINK_RE.sub("", answer).strip()
```

Keep `_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL)` and `import re`. Delete `_tail_line`.

- [ ] **Step 4: Update the caller** — in `src/tui/screens/main.py`, the StreamBlock drain (~[main.py:1163-1170](src/tui/screens/main.py#L1163)) currently does:

```python
                think, answer = sink.snapshot()
                text = format_stream_block(think=think, answer=answer)
```

Change to:

```python
                _think, answer = sink.snapshot()
                text = format_stream_block(answer=answer)
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/tui/test_stream_sink.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tui/services/stream_sink.py src/tui/screens/main.py tests/tui/test_stream_sink.py
git commit -m "feat(stream): StreamBlock shows answer only (reasoning moves to indicator)"
```

---

## Task 3: Suppress reasoning content rows

**Files:**
- Modify: `src/tui/services/trace_mapper.py:145-154`
- Modify: `src/tui/screens/main.py:742-748` (dead think mount)
- Modify: `src/tui/services/trace_render.py:195-197` (dead think render)
- Test: `tests/tui/test_trace_mapper_reasoning.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_trace_mapper_reasoning.py
from tui.services.trace_mapper import map_message


def test_reasoning_message_maps_to_none():
    ev = map_message(
        {"type": "reasoning", "content": "длинное рассуждение"},
        no=1, event_ts_utc_iso="2026-06-16T00:00:00+00:00",
    )
    assert ev is None


def test_think_message_maps_to_none():
    ev = map_message(
        {"type": "think", "summary": "рассуждение"},
        no=1, event_ts_utc_iso="2026-06-16T00:00:00+00:00",
    )
    assert ev is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/tui/test_trace_mapper_reasoning.py -v`
Expected: FAIL — currently returns a `TraceEvent(kind="system", title="think")`, not `None`.

- [ ] **Step 3: Suppress at the mapper** — in `src/tui/services/trace_mapper.py`, replace the reasoning branch (lines 145-154):

```python
    # THINK (reasoning) — kind="system" sentinel + title="think" (renders as .line-think)
    if typ in ("reasoning", "think"):
        text = msg.get("summary") or msg.get("content") or ""
        if isinstance(text, str) and text.strip():
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="system",
                title="think", summary=text.strip(),
                full_payload=msg, message_id=mid,
            )
        return None
```

with:

```python
    # THINK (reasoning) — suppressed: reasoning is shown via the ThinkingIndicator
    # tail (см. docs/tui/specs/2026-06-16-reasoning-indicator-design.md), not as a
    # trace row. (Export keeps reasoning — см. services/export.py.)
    if typ in ("reasoning", "think"):
        return None
```

- [ ] **Step 4: Remove the now-dead renders.**

In `src/tui/screens/main.py`, delete the dead think-row mount (lines 742-748):

```python
        if ev.kind == "system" and ev.title == "think":
            self._mount_trace_row(
                prefix="  ? ",
                content=f"think: {ev.summary}",
                kind_class="line-think",
                event_no=ev.no,
            )
            return
```

In `src/tui/services/trace_render.py`, delete the dead branch (lines 195-197):

```python
    elif ev.kind == "system" and ev.title == "think":
        out.append(RenderedRow(prefix="  ? ", content=f"think: {ev.summary}",
                                    kind_class="line-think", event_no=ev.no))
```

(Match the exact surrounding `elif`/structure; if removing the `elif` would orphan a dangling clause, convert the next `elif` accordingly. Read the function first.)

- [ ] **Step 5: Run to verify pass + no regression**

Run: `uv run pytest tests/tui/test_trace_mapper_reasoning.py tests/tui/ -v` (targeted; TUI snapshots may flake in full runs — run the new file in isolation if needed).
Expected: new tests PASS; no new failures attributable to this change.

- [ ] **Step 6: Commit**

```bash
git add src/tui/services/trace_mapper.py src/tui/screens/main.py src/tui/services/trace_render.py tests/tui/test_trace_mapper_reasoning.py
git commit -m "feat(tui): suppress reasoning content rows (moved to indicator)"
```

---

## Task 4: ThinkingIndicator — `с` + Claude-style reasoning tail

**Files:**
- Modify: `src/tui/widgets/thinking_indicator.py`
- Modify: `src/tui/screens/main.py:516-519` (pass sink)
- Test: `tests/tui/test_thinking_indicator_tail.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_thinking_indicator_tail.py
from tui.widgets.thinking_indicator import ThinkingIndicator


class _FakeSink:
    def __init__(self, units, state):
        self._units, self._state = units, state
    def progress_units(self):
        return self._units
    def reasoning_state(self):
        return self._state


def _plain(ind):
    return ind._render_text().plain


def test_elapsed_uses_cyrillic_s():
    ind = ThinkingIndicator(started_at=0.0, state="thinking", clock=lambda: 8.0)
    assert "(8с" in _plain(ind)
    assert "(8s" not in _plain(ind)


def test_tail_thinking_with_counter():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 8.0,
        sink=_FakeSink(1200, ("thinking", None)),
    )
    p = _plain(ind)
    assert "↓ 1.2k токенов" in p
    assert "размышление" in p


def test_tail_done_shows_obdumal():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 31.0,
        sink=_FakeSink(3300, ("done", 6.0)),
    )
    p = _plain(ind)
    assert "↓ 3.3k токенов" in p
    assert "обдумал за 6с" in p


def test_no_tail_without_sink():
    ind = ThinkingIndicator(started_at=0.0, state="thinking", clock=lambda: 40.0)
    p = _plain(ind)
    assert "токенов" not in p
    assert "размышление" not in p


def test_no_reasoning_tag_when_state_none():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 40.0,
        sink=_FakeSink(4100, (None, None)),
    )
    p = _plain(ind)
    assert "↓ 4.1k токенов" in p
    assert "размышление" not in p
    assert "обдумал" not in p
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/tui/test_thinking_indicator_tail.py -v`
Expected: FAIL — no `clock`/`sink` params; elapsed uses `s`; no tail.

- [ ] **Step 3: Implement** — in `src/tui/widgets/thinking_indicator.py`:

(a) Add a units formatter near the top (after the constants):

```python
def _fmt_units(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
```

(b) Extend `__init__` (add `clock` and `sink`, store them):

```python
    def __init__(
        self,
        *,
        started_at: float | None = None,
        state: str = _STATE_THINKING,
        clock=time.monotonic,
        sink=None,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._started_at: float = (
            started_at if started_at is not None else clock()
        )
        self._frame: int = 0
        self._state: str = state
        self._rotator = PhraseRotator(THINKING_PHRASES)
        super().__init__(self._render_text(), classes="thinking-indicator", markup=False)
```

(c) Replace the elapsed tail at the end of `_render_text` (the current `elapsed = int(time.monotonic() - self._started_at)` + `text.append(f" ({elapsed}s)")`) with:

```python
        elapsed = int(self._clock() - self._started_at)
        parens = f" ({elapsed}с"
        if self._state == _STATE_THINKING and self._sink is not None:
            units = self._sink.progress_units()
            if units > 0:
                parens += f" · ↓ {_fmt_units(units)} токенов"
            r_state, dur = self._sink.reasoning_state()
            if r_state == "thinking":
                parens += " · размышление"
            elif r_state == "done":
                parens += f" · обдумал за {round(dur)}с"
        parens += ")"
        text.append(parens)
        return text
```

- [ ] **Step 4: Wire the sink in main.py** — at [main.py:516-519](src/tui/screens/main.py#L516), change the construction:

```python
        from tui.widgets.thinking_indicator import ThinkingIndicator
        indicator = ThinkingIndicator(
            started_at=self._thinking_started_at, state=state,
            sink=getattr(self.app, "_stream_sink", None),
        )
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/tui/test_thinking_indicator_tail.py -v`
Expected: PASS (all 5)

- [ ] **Step 6: Commit**

```bash
git add src/tui/widgets/thinking_indicator.py src/tui/screens/main.py tests/tui/test_thinking_indicator_tail.py
git commit -m "feat(tui): ThinkingIndicator reasoning tail (с · ↓ N токенов · размышление/обдумал за Nс)"
```

---

## Task 5: Manual end-to-end verification

**Files:** none (manual; needs a streaming reasoning endpoint).

- [ ] **Step 1: Run the TUI** against a `chat_completions` endpoint that streams `reasoning_content` (or inline `<think>`), `backend.stream: true`. Send a prompt that triggers reasoning.
Expected:
- **No** `think: …` reasoning rows in the trace.
- Indicator shows `⠹ <фраза>… (Nс · ↓ N токенов · размышление)` while reasoning; counter `↓ N` **grows** as deltas arrive.
- After reasoning ends: `… (Nс · ↓ N токенов · обдумал за Mс)` (sticky).
- The live answer still streams in the StreamBlock (answer only, no reasoning text).

- [ ] **Step 2: No-reasoning / stream-off checks**
- Model/turn without reasoning → indicator `⠹ <фраза>… (Nс · ↓ N токенов)` (no reasoning tag).
- `backend.stream: false` → indicator `⠋ <фраза>… (Nс)` (no tail); no reasoning rows.

- [ ] **Step 3: Targeted regression**

Run: `uv run pytest tests/tui/test_stream_sink.py tests/tui/test_thinking_indicator_tail.py tests/tui/test_trace_mapper_reasoning.py tests/tui/test_stream_block_render.py -v`
Expected: all PASS.

---

## Self-Review notes (for the implementer)

- **Spec coverage:** §3/§5 format → Task 4 (`с`, order время·счётчик·состояние, «токенов», `обдумал за Nс`). §4 StreamSink → Task 1. §6.1 suppress rows → Task 3. §6.2 inline `<think>` strip in agent text → **already done** (`_extract_assistant_text`, trace_mapper.py:75) — no task. §6.3 StreamBlock answer-only → Task 2. §7 edge cases covered by Task 1 (`reasoning_state` transitions, reset) + Task 4 (no-sink/no-reasoning tail). §8 tests → Tasks 1-4.
- **Type consistency:** `reasoning_state()` returns `(None,None) | ("thinking",None) | ("done",float)` — same in Task 1 (sink), Task 4 (indicator reads it), Task 4 tests (FakeSink). `progress_units() -> int`. `format_stream_block(*, answer)` — Task 2 signature matches the main.py caller update.
- **No placeholders:** every code/step is concrete. The only "read first" is Task 3 Step 4 (confirm the exact `elif` structure in `trace_render.py` before deleting) — a precise, scoped instruction, not a vague gap.

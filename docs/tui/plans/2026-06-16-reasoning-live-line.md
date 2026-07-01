# Reasoning live line (Revision 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show reasoning as a single compact dim line above the indicator (not full content, not nothing): detect inline `<think>` in the stream, surface a rolling tail line via the StreamBlock, and drop the indicator's «· размышление» word (rotation runs as-is).

**Architecture:** Extends the just-built reasoning-indicator (same branch). The SSE parser gains a stateful inline-`<think>` splitter routing think-segments to the "think" channel. The StreamBlock shows the answer when present, else a compact reasoning tail line (`⋮ …`, dim/italic). The indicator keeps counter + «обдумал за Nс» but loses the «размышление» word.

**Tech Stack:** Python ≥3.10, pytest, Textual (TUI).

**Spec:** `docs/tui/specs/2026-06-16-reasoning-indicator-design.md` → section «Ревизия 2».
**Branch:** `feat/tui-stream-chat-completions` (continue here).

---

## File Structure

- **Modify** `src/utils/chat_completions_stream.py` — add `_InlineThinkSplitter` + `_partial_prefix_len`; in `assemble_streamed_completion`, route streamed `content` through the splitter (think/answer) instead of emitting the whole chunk as "answer". Final `message.content` stays verbatim.
- **Modify** `src/tui/services/stream_sink.py` — add `format_reasoning_line(*, think, tail_len=64)` + `_reasoning_tail` helper. `format_stream_block` (answer-only) stays.
- **Modify** `src/tui/widgets/stream_block.py` — `set_text(text, *, thinking=False)` toggles a `.stream-block-think` class.
- **Modify** `src/tui/styles.tcss` — add `.stream-block-think` (dim/italic).
- **Modify** `src/tui/screens/main.py` — drain: show answer if present, else the reasoning line.
- **Modify** `src/tui/widgets/thinking_indicator.py` — remove the `· размышление` branch (keep `· обдумал за Nс`).
- **Tests** under `tests/` and `tests/tui/`.

---

## Task 1: Inline-`<think>` splitter in the SSE parser

**Files:**
- Modify: `src/utils/chat_completions_stream.py`
- Test: `tests/test_chat_completions_inline_think.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_completions_inline_think.py
from utils.chat_completions_stream import iter_sse_data_lines, assemble_streamed_completion


def _deltas(lines):
    out = []
    assemble_streamed_completion(iter_sse_data_lines(lines), on_delta=lambda k, t: out.append((k, t)))
    return out


def test_inline_think_split_within_one_chunk():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"<think>думаю тут</think>Ответ."}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    assert ("think", "думаю тут") in d
    assert ("answer", "Ответ.") in d
    # no raw tags leak into emitted segments
    assert all("<think>" not in t and "</think>" not in t for _, t in d)


def test_inline_think_tag_split_across_chunks():
    # "<think>" arrives split as "<thi" + "nk>"; close split too.
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"<thi"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"nk>раз"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"мышляю</thi"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"nk>го"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"тово"}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    think = "".join(t for k, t in d if k == "think")
    answer = "".join(t for k, t in d if k == "answer")
    assert think == "размышляю"
    assert answer == "готово"


def test_unclosed_think_all_routed_to_think():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"<think>всё ещё думаю"}}]}',
        'data: [DONE]',
    ]
    d = _deltas(lines)
    assert "".join(t for k, t in d if k == "think") == "всё ещё думаю"
    assert "".join(t for k, t in d if k == "answer") == ""


def test_reasoning_content_still_think_and_message_content_verbatim():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"rc"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"<think>x</think>Y"}}],"choices":[{"index":0,"delta":{"content":""}}]}',
        'data: [DONE]',
    ]
    # second line is intentionally simple; assemble keeps message.content verbatim incl. <think>
    lines = [
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"rc"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"<think>x</think>Y"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    out = []
    res = assemble_streamed_completion(iter_sse_data_lines(lines), on_delta=lambda k, t: out.append((k, t)))
    assert ("think", "rc") in out
    assert ("think", "x") in out
    assert ("answer", "Y") in out
    # message.content keeps the inline <think> verbatim (downstream strips it)
    assert res["choices"][0]["message"]["content"] == "<think>x</think>Y"
```

- [ ] **Step 2: Run, verify FAIL**

Run: `pytest tests/test_chat_completions_inline_think.py -v` (fallback `python -m pytest`)
Expected: FAIL — currently the whole `content` chunk is emitted as one `("answer", …)`, inline `<think>` not split.

- [ ] **Step 3: Implement the splitter**

In `src/utils/chat_completions_stream.py`, add after the `OnDelta` type alias (line 12):

```python
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _partial_prefix_len(buf: str, tag: str) -> int:
    """Longest suffix of `buf` that is a proper prefix of `tag` (a tag possibly
    split across chunk boundaries — hold it back until the next chunk)."""
    for k in range(min(len(buf), len(tag) - 1), 0, -1):
        if tag.startswith(buf[-k:]):
            return k
    return 0


class _InlineThinkSplitter:
    """Route streamed `content` into think/answer segments, tracking
    `<think>...</think>` across chunk boundaries. Tags split between chunks are
    buffered until resolved. Tag characters themselves are consumed (not emitted)."""

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, text: str, emit: OnDelta) -> None:
        self._buf += text
        while self._buf:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            idx = self._buf.find(tag)
            if idx != -1:
                before = self._buf[:idx]
                if before:
                    emit("think" if self._in_think else "answer", before)
                self._buf = self._buf[idx + len(tag):]
                self._in_think = not self._in_think
                continue
            keep = _partial_prefix_len(self._buf, tag)
            cut = len(self._buf) - keep
            flush_now = self._buf[:cut]
            if flush_now:
                emit("think" if self._in_think else "answer", flush_now)
            self._buf = self._buf[cut:]
            break

    def flush(self, emit: OnDelta) -> None:
        if self._buf:
            emit("think" if self._in_think else "answer", self._buf)
            self._buf = ""
```

- [ ] **Step 4: Integrate into `assemble_streamed_completion`**

In `assemble_streamed_completion`, after the local vars (`resp_id = None`, line 49) add:

```python
    splitter = _InlineThinkSplitter() if on_delta is not None else None
```

Replace the content-emit block (lines 85-89):

```python
        c = delta.get("content")
        if isinstance(c, str) and c:
            content_parts.append(c)
            if on_delta is not None:
                on_delta("answer", c)
```

with (still append raw `c` to `content_parts`; route deltas through the splitter):

```python
        c = delta.get("content")
        if isinstance(c, str) and c:
            content_parts.append(c)
            if splitter is not None:
                splitter.feed(c, on_delta)
```

After the `for payload in data_payloads:` loop ends (before building `message`, i.e. before line 110), add:

```python
    if splitter is not None:
        splitter.flush(on_delta)
```

(reasoning_content → `on_delta("think", rc)` stays unchanged. `content_parts` still gets raw `c`, so `message.content` keeps inline `<think>` verbatim.)

- [ ] **Step 5: Run, verify PASS** (new file + existing parser tests)

Run: `pytest tests/test_chat_completions_inline_think.py tests/test_chat_completions_stream.py -v`
Expected: PASS. The existing `test_assemble_reassembles_message_content_and_tool_calls` still passes (its content has no `<think>`, so segments route to "answer" as before).

- [ ] **Step 6: Commit**

```bash
git add src/utils/chat_completions_stream.py tests/test_chat_completions_inline_think.py
git commit -m "feat(stream): split inline <think> in content stream into think channel"
```

---

## Task 2: `format_reasoning_line` (compact tail)

**Files:**
- Modify: `src/tui/services/stream_sink.py`
- Test: `tests/tui/test_stream_sink.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/tui/test_stream_sink.py`)

```python
def test_reasoning_line_short_unchanged():
    from tui.services.stream_sink import format_reasoning_line
    assert format_reasoning_line(think="смотрю agents/") == "⋮ смотрю agents/"


def test_reasoning_line_collapses_newlines():
    from tui.services.stream_sink import format_reasoning_line
    assert format_reasoning_line(think="строю план\nчитаю README") == "⋮ строю план читаю README"


def test_reasoning_line_truncates_with_ellipsis():
    from tui.services.stream_sink import format_reasoning_line
    out = format_reasoning_line(think="a" * 100, tail_len=10)
    assert out == "⋮ …" + "a" * 10


def test_reasoning_line_empty():
    from tui.services.stream_sink import format_reasoning_line
    assert format_reasoning_line(think="") == ""
    assert format_reasoning_line(think="   ") == ""
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/tui/test_stream_sink.py -k reasoning_line -v` (fallback `python -m pytest`)
Expected: FAIL — `format_reasoning_line` does not exist.

- [ ] **Step 3: Implement** — in `src/tui/services/stream_sink.py`, add after `format_stream_block` (do not change `format_stream_block`):

```python
def _reasoning_tail(think: str, maxlen: int) -> str:
    """Reasoning as a single line: collapse whitespace; '…'-prefixed last
    `maxlen` chars if longer."""
    one_line = " ".join(think.split())
    if not one_line:
        return ""
    if len(one_line) <= maxlen:
        return one_line
    return "…" + one_line[-maxlen:]


def format_reasoning_line(*, think: str, tail_len: int = 64) -> str:
    """Compact one-line reasoning glimpse for the StreamBlock during the
    thinking phase: `⋮ <tail>` (empty string if no reasoning)."""
    tail = _reasoning_tail(think, tail_len)
    return f"⋮ {tail}" if tail else ""
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/tui/test_stream_sink.py -v`
Expected: PASS (new tests + all existing).

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/stream_sink.py tests/tui/test_stream_sink.py
git commit -m "feat(stream): format_reasoning_line (compact one-line reasoning tail)"
```

---

## Task 3: StreamBlock thinking style

**Files:**
- Modify: `src/tui/widgets/stream_block.py`
- Modify: `src/tui/styles.tcss`
- Test: `tests/tui/test_stream_block_thinking_class.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_stream_block_thinking_class.py
import pytest
from textual.app import App
from tui.widgets.stream_block import StreamBlock


@pytest.mark.asyncio
async def test_set_text_toggles_thinking_class():
    class _App(App):
        def compose(self):
            yield StreamBlock()

    app = _App()
    async with app.run_test():
        block = app.query_one(StreamBlock)
        block.set_text("⋮ думаю", thinking=True)
        assert block.has_class("stream-block-think")
        block.set_text("обычный ответ", thinking=False)
        assert not block.has_class("stream-block-think")
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/tui/test_stream_block_thinking_class.py -v` (fallback `python -m pytest`)
Expected: FAIL — `set_text` has no `thinking` param / class not toggled.

- [ ] **Step 3: Implement** — replace `set_text` in `src/tui/widgets/stream_block.py`:

```python
    def set_text(self, text: str, *, thinking: bool = False) -> None:
        if not self.is_mounted:
            return
        if thinking:
            self.add_class("stream-block-think")
        else:
            self.remove_class("stream-block-think")
        self.update(text)
```

In `src/tui/styles.tcss`, add right after the `.stream-block` rule (line 192):

```css
.stream-block-think { color: $muted; text-style: italic; }
```

(The widget keeps the `stream-block` class always, so padding/margin-top from `.stream-block` still apply; `.stream-block-think` only overrides color + adds italic.)

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/tui/test_stream_block_thinking_class.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tui/widgets/stream_block.py src/tui/styles.tcss tests/tui/test_stream_block_thinking_class.py
git commit -m "feat(tui): StreamBlock thinking style toggle (dim italic)"
```

---

## Task 4: Drain — show answer or reasoning line

**Files:**
- Modify: `src/tui/screens/main.py` (drain block ~1153-1162)
- Test: covered by Task 5's integration + existing `test_stream_block_render.py` (verify no regression)

- [ ] **Step 1: Update the drain block.** In `src/tui/screens/main.py`, the current block (lines 1153-1162):

```python
            sink = getattr(self.app, "_stream_sink", None)
            if sink is not None and sink.has_content():
                from tui.services.stream_sink import format_stream_block
                from tui.widgets.stream_block import StreamBlock
                _think, answer = sink.snapshot()
                text = format_stream_block(answer=answer)
                if self._stream_block is None:
                    self._stream_block = StreamBlock()
                    self._trace_mount(self._stream_block, scroll=True)
                self._stream_block.set_text(text)
```

Replace with:

```python
            sink = getattr(self.app, "_stream_sink", None)
            if sink is not None and sink.has_content():
                from tui.services.stream_sink import (
                    format_stream_block, format_reasoning_line,
                )
                from tui.widgets.stream_block import StreamBlock
                think, answer = sink.snapshot()
                ans = format_stream_block(answer=answer)
                if ans:
                    text, thinking = ans, False
                else:
                    text, thinking = format_reasoning_line(think=think), True
                if self._stream_block is None:
                    self._stream_block = StreamBlock()
                    self._trace_mount(self._stream_block, scroll=True)
                self._stream_block.set_text(text, thinking=thinking)
```

- [ ] **Step 2: Run existing StreamBlock render tests (no regression)**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: PASS. NOTE: `test_block_text_from_sink` asserts the block text for a sink with think+answer. After this change + Task 1, with both think and answer present the block shows the ANSWER (answer non-empty → `ans` truthy). If that test pushes only think (no answer), it should now expect the `⋮ …` reasoning line. READ the test; update its expectation to match: if it sets only an answer → expect answer; if only think → expect `⋮ <tail>`. Keep assertions tight (exact strings), not weakened.

- [ ] **Step 3: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_stream_block_render.py
git commit -m "feat(tui): StreamBlock shows reasoning line when no answer yet"
```

---

## Task 5: Drop «· размышление» from the indicator

**Files:**
- Modify: `src/tui/widgets/thinking_indicator.py:181-185`
- Test: `tests/tui/test_thinking_indicator_tail.py` (update)

- [ ] **Step 1: Update the failing test.** In `tests/tui/test_thinking_indicator_tail.py`, the existing `test_tail_thinking_with_counter` asserts `"размышление" in p`. Replace its body with:

```python
def test_tail_thinking_no_word_but_counter():
    ind = ThinkingIndicator(
        started_at=0.0, state="thinking", clock=lambda: 8.0,
        sink=_FakeSink(1200, ("thinking", None)),
    )
    p = _plain(ind)
    assert "↓ 1.2k токенов" in p
    assert "размышление" not in p   # word dropped; reasoning shown via the StreamBlock line
```

Keep `test_tail_done_shows_obdumal`, `test_no_tail_without_sink`, `test_no_reasoning_tag_when_state_none`, `test_elapsed_uses_cyrillic_s` unchanged.

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/tui/test_thinking_indicator_tail.py -v` (fallback `python -m pytest`)
Expected: FAIL on the renamed test — `· размышление` is still appended.

- [ ] **Step 3: Implement** — in `src/tui/widgets/thinking_indicator.py`, the current tail (lines 181-185):

```python
            r_state, dur = self._sink.reasoning_state()
            if r_state == "thinking":
                parens += " · размышление"
            elif r_state == "done":
                parens += f" · обдумал за {round(dur)}с"
```

Replace with (drop the `thinking` branch; keep `done`):

```python
            _r_state, dur = self._sink.reasoning_state()
            if _r_state == "done":
                parens += f" · обдумал за {round(dur)}с"
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/tui/test_thinking_indicator_tail.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/tui/widgets/thinking_indicator.py tests/tui/test_thinking_indicator_tail.py
git commit -m "feat(tui): drop '· размышление' word (reasoning shown via StreamBlock line)"
```

---

## Task 6: Ordering test (ask_human) + manual e2e

**Files:**
- Test: `tests/tui/test_stream_block_render.py` (append ordering test)
- Manual.

- [ ] **Step 1: Append an ordering test** to `tests/tui/test_stream_block_render.py` — model it on the existing integration test in that file (App.run_test() pilot + fake runner). The test should: render an ask_human exchange (question + answer rows committed), then push reasoning deltas to `app._stream_sink`, tick `_poll_active`, and assert the `StreamBlock` (reasoning line) is the LAST trace child (above only the ThinkingIndicator), i.e. its position index is greater than the committed ask_human rows.

```python
# Sketch — adapt to the harness/fixtures already used by the integration test
# at the top of this file (App.run_test pilot, fake runner, _trace children order):
@pytest.mark.asyncio
async def test_reasoning_line_stays_below_committed_rows(<tui_harness>):
    # 1. commit some rows incl. an answered ask_human (use the same helpers the
    #    existing integration test uses to mount canonical rows)
    # 2. app._stream_sink.push("think", "новый шаг рассуждения")
    # 3. drive a _poll_active tick (pilot.pause >= 2*0.25s)
    # 4. find the StreamBlock and the last committed row in the #trace VerticalScroll
    #    children; assert StreamBlock index > committed-row index (i.e. below them),
    #    and only the ThinkingIndicator (if any) is after it.
    ...
```

> If the existing harness can't easily assert child ordering, instead assert: after the reasoning line is shown, mounting a canonical row (via the same path the integration test uses) clears the reasoning line (`_clear_stream_block`) so it never persists above newer rows. Match whichever assertion the existing integration test style supports. Run in isolation (snapshot flakiness).

- [ ] **Step 2: Run**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/tui/test_stream_block_render.py
git commit -m "test(tui): reasoning line stays below committed rows (ask_human ordering)"
```

- [ ] **Step 4: Manual e2e** — run the TUI against the streaming reasoning model (the gigacode/SGLang one that emits inline `<think>`):
  - During reasoning: a dim/italic `⋮ …<rolling tail>` line appears above the indicator; the indicator shows `⠹ <фраза>… (Nс · ↓ N токенов)` with NO «размышление» word, counter grows.
  - After reasoning: the line is replaced by the answer (or the turn's tool call/report); indicator shows `· обдумал за Nс`.
  - After an answered ask_human, a new reasoning step's line appears BELOW the ask_human rows (not above).

---

## Self-Review notes (for the implementer)

- **Spec coverage (Ревизия 2):** R2.1 inline-`<think>` detect → Task 1. R2.2 reasoning tail line + StreamBlock display → Tasks 2,3,4. R2.3 drop «· размышление», keep «обдумал за Nс» → Task 5. R2.4 lifecycle/ordering → Task 4 (reuses StreamBlock mount/clear) + Task 6 (ordering test). R2.5 edges → Task 1 (unclosed think), Task 2 (empty), Task 4 (answer vs reasoning). R2.6 tests → Tasks 1-6.
- **Type consistency:** `format_reasoning_line(*, think, tail_len=64) -> str`, `format_stream_block(*, answer) -> str` (unchanged), `StreamBlock.set_text(text, *, thinking=False)`. on_delta contract `("think"|"answer", str)` unchanged — Task 1 just routes content through the splitter. `reasoning_state()` unchanged (Task 5 reads it, drops only the thinking-word output).
- **No placeholders:** all code concrete. The only "read & adapt" is Task 6's harness-dependent ordering assertion (and Task 4 Step 2 updating `test_block_text_from_sink` to the new answer-vs-reasoning behavior) — precise, scoped instructions, not vague gaps.

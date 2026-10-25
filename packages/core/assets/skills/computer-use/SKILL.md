---
id: "computer-use"
name: "Computer Use — Windows GUI control"
description: "You MUST load this skill BEFORE any call to computer_* tools (computer_launch_app, computer_click, computer_type, computer_get_state, computer_scroll, computer_shortcut, computer_key, computer_switch, computer_resize, computer_drag, computer_wait). Triggers in the user's request: 'открой/запусти/закрой приложение X', 'кликни/нажми кнопку/ссылку/меню', 'введи/набери/напечатай текст', 'сохрани файл', 'переключись на окно', 'прокрути', 'нажми сочетание клавиш'. Applications: Excel, Word, Outlook, PowerPoint, Блокнот, Калькулятор, 1С, Chrome, internal corporate applications. This skill covers tool-call ordering, using element_id from get_state, and handling common errors."
version: "0.1.2"
---

# Computer Use — Windows GUI control

## When to load this skill

**Always load `skills_load("computer-use")` as soon as you determine that the task
requires any `computer_*` tool.** This is the only source of tool-call ordering
rules — without it, you will call tools in the wrong order and get errors.

Load it **before** the first `computer_*` call, not afterward. Loading takes
one tool call and pays off by avoiding rework caused by
[stale_state] / [element_not_found].

## Purpose

Instructions for working with Windows desktop applications through the
`gc-computer-mcp` MCP server. Applies when an application does not provide a
programmatic API — Excel, Word, Outlook, 1С, internal corporate tools.

If `computer_*` tools are not available, the MCP server is not connected
and this skill does not apply; inform the user.

## How interaction works

You do not see the screen directly. You receive a **structured textual
description** of the UI through `computer_get_state`. The response contains
an element tree with integer IDs in square brackets, types (Button, TextBox,
MenuItem), and text.

Example tree:

```
[0] Desktop
  [1] Window "Microsoft Excel — Книга1"
    [2] Button "Сохранить"
    [3] Edit "A1"
    [4] Menu "Файл"
    [5] Edit "Поиск" value="last query"
```

You interact with elements **by their IDs**:

- `computer_click(element_id=2)` — click the “Сохранить” button
- `computer_type(element_id=3, text="Продажи")` — enter text in the empty A1 field

**The `value="..."` field on Edit/ComboBox/Document** shows the field's current
contents (if nonempty). If `value=` is absent from the line, the field is empty.
This is a key signal for `computer_type` (see the “Form fields” section below).

## Mandatory sequence

For **any** GUI action, follow this order:

1. **`computer_launch_app(...)`** — if the required application is not already running.
2. **`computer_wait(1)` or `computer_wait(2)`** — let the window render.
   Skipping this step is a common cause of `[element_not_found]`.
3. **`computer_get_state()`** — obtain the current element tree and its
   `element_id` values. **Without this step, you do not know which IDs exist**,
   and any attempt to use `computer_click`/`computer_type` with a guessed
   `element_id` will fail with `[stale_state]` or `[element_not_found]`.

   **The `scope` parameter.** By default, `get_state` returns the tree of the
   currently focused window (`scope="foreground"`), saving tokens and targeting
   more precisely. For a list of all open windows, use `scope="desktop"`.
   If you know the target window's title, use `scope="window"`,
   `window_title="подстрока"`.
4. **`computer_click` / `computer_type` / `computer_scroll` / `computer_key`
   / `computer_shortcut`** — perform the action using an `element_id` from
   the latest `get_state`.
5. **Repeat `computer_get_state` if more actions are planned**
   — IDs may change after each mutating call.

## Operating rules

1. **Always start with `computer_get_state`.** Without it, you do not know
   which elements exist on the screen. Do not call `computer_click`
   / `computer_type` / `computer_scroll` without a preceding successful
   `computer_get_state` in the same sequence of steps.

2. **Call `computer_get_state` only when the screen STRUCTURE may have
   changed.** This means:
   - a menu, dialog, or popup panel has opened / closed;
   - the application window, mode, or tab has changed;
   - controls have appeared / disappeared.

   **An extra `get_state` is NOT needed between consecutive clicks or
   typing actions on the same screen** — for example, a series of digit and
   operator clicks in a calculator, typing several letters with
   `computer_type`, or consecutive `computer_key` calls. The layout does
   not change, and the buttons retain the `element_id` values from the
   latest snapshot. Extra snapshots waste tokens and add unnecessary
   round trips between you and the application.

   If in doubt, obtain `get_state` rather than click a stale `element_id`.
   But do not do so automatically after every individual click.

   **Always verify the result through `get_state` after:**
   - `computer_shortcut(["ctrl","v"])` — check whether the text was pasted
     (look at `value="..."` on the target `Edit`);
   - `computer_shortcut(["ctrl","s"])` — check whether a save dialog opened
     or the file was saved;
   - `computer_key("enter")` in a message / search field or dialog —
     check whether the message was sent, the search applied, or the dialog closed;
   - any `computer_shortcut` in an Electron / Qt application (Telegram,
     VS Code, Jazz, Teams) — these applications often swallow keyboard
     input, and the tools return `ok=true` even though nothing happened.

   `ok=true` from shortcut/key means only that SendInput sent the keys
   to the system, NOT that the application reacted.

3. **Use keyboard shortcuts through `computer_shortcut` wherever
   possible.** They are faster and more reliable than a series of clicks:
   `["ctrl","s"]` instead of clicking “Файл → Сохранить”. `["ctrl","c"]`,
   `["ctrl","v"]`, `["alt","tab"]`, `["ctrl","shift","s"]` — familiar shortcuts work.

4. **Use `computer_launch_app(имя)` to launch applications**
   with short names: `excel`, `word`, `outlook`, `notepad`, `calc`,
   `powerpoint`, `explorer`, `cmd`. Do not look for desktop icons through
   clicks and `computer_get_state`.

5. **Before destructive actions** (deletion, closing without saving,
   sending messages, irreversible changes), ask the user for confirmation.

6. **Do not repeat the same action indefinitely.** If an action has
   failed three times, try an alternative route or inform the user
   of the problem.

7. **If `computer_get_state` returns an almost empty tree or only
   PaneControl without content**, the application most likely does not
   support UIA (Electron: Teams, Slack, VS Code; canvas-based UI; games).
   Inform the user; if `computer_capture_screenshot` /
   `computer_analyze_screen_region` are available in the tool list,
   try them.

8. **Electron applications require explicit focus before `get_state`.**
   Jazz / SberJazz, Teams, Slack, Discord, VS Code, and other Electron
   applications materialize their UIA content only when the window has focus.
   Pattern:
   `computer_launch_app(...)` → `computer_wait(2)` → `computer_switch(window_title=...)` →
   `computer_get_state()`. Skipping `computer_switch` yields an almost empty tree.

9. **Do not call computer tools in parallel within a single model step.**
   The server stores the snapshot in `%TEMP%\gc-computer-mcp\last_state_*.json`,
   and parallel `computer_get_state` calls compete for it.
   Ordering matters more than speed.

## Common errors and how to respond

All computer tools return errors with a `[category]` prefix in the message.
The category is a contract: use it to decide what to do.

- `[element_not_found]` or `[stale_state]` → immediately call
  `computer_get_state` and retry the action using new IDs.
- `[readonly_violation]` → do not retry; readonly mode is intentional.
  Inform the user if they expected a mutation.
- `[blacklisted_keys]` → do not retry that combination. It is on the system
  blacklist (ctrl+alt+del, win+l, alt+f4). Choose another strategy.
- `[timeout]` → the tool exceeded its time limit. The system may be frozen
  or the response too large. Try `computer_wait(5)` and retry.
- `[app_not_found]` → the application was not found or no window has that
  title. Check the exact spelling and use `computer_get_state`
  to see the list of active windows.
- `[out_of_bounds]` → coordinates are outside the screen. Call `get_state`
  and use the current element bounds.

## If vision tools are available

If `computer_capture_screenshot` and `computer_analyze_screen_region`
are activated in the environment, use them for:

- Analyzing the contents of graphs and charts
- Working with applications whose UIA tree is empty or uninformative
- Visually verifying an action's result

**Do not use vision tools for anything that can be handled through UIA** —
it is slower, costs more tokens, and is not always more reliable.

**Important:** in the current background-agent version, MCP binary data
reaches the LLM as the string `"binary data"`, not as images. The vision
tools are ready to use, but their output does not yet reach the model
as images. Warn the user if their request requires visual analysis.

## Available tools

Primary (UIA, always available if MCP is connected):

| Tool | Signature (key parameters) |
|---|---|
| `computer_get_state` | `scope="foreground"` (default) / `"window"` / `"desktop"`, `window_title?`, `max_depth?`, `exclude_shell`, `only_interactive`, `include_invisible?` |
| `computer_click` | `element_id`, `button?`, `clicks?` |
| `computer_type` | `element_id`, `text`, `clear?` (see “Form fields”), `press_enter?` |
| `computer_scroll` | `direction`, `amount?`, `element_id?` |
| `computer_drag` | `from_element_id`, `to_element_id`, `button?` |
| `computer_key` | `key` (a single key: `enter`, `f5`, `tab`) |
| `computer_shortcut` | `keys: list[str]` (`["ctrl","s"]`) |
| `computer_resize` | `window_title`, `x?/y?/width?/height?/maximize?/minimize?` |
| `computer_switch` | `window_title` (substring) |
| `computer_launch_app` | `app_name` (`excel`, `word`, `outlook`, `notepad`, ...) |
| `computer_wait` | `seconds` (0..30) |

Optional (vision, activated by a flag in the MCP server environment):

| Tool | Signature |
|---|---|
| `computer_capture_screenshot` | `region?`, `downscale?` |
| `computer_analyze_screen_region` | `x`, `y`, `width`, `height` |

## Form fields

Before `computer_type(element_id=X)`, look at element `X` in the tree
from `get_state` and choose among three cases:

**Case 1 — the user supplied specific text for the field.**
Requests such as “введи 'Привет' в блокноте”, “заполни имя 'Иван'”.
→ `computer_type(element_id=X, text="Привет", clear=True)` — always use
`clear=True` to avoid concatenating it with existing text.

**Case 2 — the user did NOT supply text and the field already contains
a meaningful value (you see `value="..."` on the element).**
A typical example is an Electron form autofilled from a previous session
(SberJazz remembers the name “Дмитрий” and title “Видеовстреча”).
→ **Do not call `computer_type` at all.** Leave the field as it is.
Inventing a default and overwriting meaningful autofill is worse
than doing nothing.

**Case 3 — the user did not supply text, the field is empty (no
`value="..."` in the tree), and it appears required.**
→ First try to continue without filling it (many forms allow required
fields with empty defaults on the server). Only if validation blocks
progress, use a short neutral value (`"Пользователь"`, `"Встреча"`)
and pass `clear=True`.

### How to read `value=` in the tree

```
[49] Edit "Ваше имя" value="Дмитрий"     ← field is populated
[56] Edit "Название встречи"             ← field is empty
[64] Button "Создать и присоединиться"
```

- `value="..."` is present → the field already contains something.
- No `value=` → the field is empty (or it is not an Edit/ComboBox — check its type).

### Anti-pattern

Do not do this:
```
# The user only said “создай встречу” and did not provide a name.
computer_type(element_id=49, text="Пользователь")  # overwrites "Дмитрий"
computer_type(element_id=56, text="Моя встреча")   # overwrites "Видеовстреча"
```
This produces concatenation such as `"ДмитрийПользователь"` or loses
meaningful autofill. The correct behavior is to **leave these fields
untouched** and click the final button immediately.

### Focus the field before Ctrl+V / Ctrl+A

To focus an input (for example, before `computer_shortcut(["ctrl","v"])`),
use **`computer_click(element_id=X)`**. Do not use `computer_type(text="")`
as a focusing mechanism: it still performs click + SendInput with an empty
string, but is less obvious in the log and easily confused with actual text input.

## Using the window returned by `computer_launch_app`

`computer_launch_app` returns `window_title` and `window_hwnd` if it can
identify them within ~3 seconds. After `launch_app`, you can therefore call
`computer_get_state(scope="window", window_title=<launched_title>)` directly,
bypassing expensive foreground polling. Example:

1. `computer_launch_app("Jazz")` → `{launched: "Jazz", window_title: "SaluteJazz", ...}`
2. `computer_wait(2)` (Electron still needs time)
3. `computer_get_state(scope="window", window_title="SaluteJazz")`

## Example scenarios

**Open the calculator and calculate 2+2:**
1. `computer_launch_app("calc")`
2. `computer_wait(1)` — let the window open
3. `computer_get_state()` — see the calculator buttons
4. `computer_click(element_id=<2>)`, `computer_click(element_id=<+>)`,
   `computer_click(element_id=<2>)`, `computer_click(element_id=<=>)`
5. `computer_get_state()` — read the result from the screen

**Create a spreadsheet in Excel:**
1. `computer_launch_app("excel")`
2. `computer_wait(2)` — Excel takes noticeably longer to launch
3. `computer_get_state()` — expect to see the grid and cell A1
4. `computer_type(element_id=<A1>, text="Продажи")` — enter text in the cell
5. `computer_key("tab")` — move to B1
6. ... and so on
7. `computer_shortcut(["ctrl","s"])` — save

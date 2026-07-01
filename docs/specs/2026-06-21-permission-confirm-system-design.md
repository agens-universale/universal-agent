# Permission / Confirm System — Design (Mid, Full-ready)

Дата: 2026-06-21
Статус: design (готов к плану)
Ветка: `feat/layered-instructions-and-gc-protection`
Связанные спеки: `docs/specs/2026-06-20-gc-protection-and-command-guard-design.md`,
`docs/specs/2026-06-20-layered-agent-instructions-and-init-design.md`

## 0. Проблема и цель

Гарды (`write_guard`, `read_guard`, `url_policy`, `command_moderation`, `command_guard`)
в режиме `ask` зовут `confirm_port.request_confirm`. Сейчас подтверждение бинарно и
**без памяти**: на «Нет» агент (особенно слабые модели) ретраит тот же вызов, гард
снова срабатывает, баннер переоткрывается — пользователя «долбит», пока он не сдастся
и не нажмёт «Да».

Цель — **Mid-уровень** системы разрешений «как в зрелых харнесах», но без режимов
(`default/auto-accept/plan/bypass`) и без glob/scope-иерархии (это Full, §7). Mid
решает боль и даёт durable-контроль, оставляя чистый шов под Full.

Что уже есть (контекст): моды `allow/ask/deny` у 4 слоёв — **из мастера**;
`confirm_port`, `protected_paths`, `command_guard`, TUI confirm-баннер (3-значного
выбора пока нет, только Y/N) — добавлены этой веткой.

## 1. Поведение для пользователя

Confirm-баннер — **вертикальная** раскладка, три варианта, точное короткое описание:

```
? Write guard
  Разрешить запись в D:\dev\sber\skills\test.txt?
  Tool:          fs_write_file
  Path:          D:\dev\sber\skills\test.txt
  Allowed roots: D:\dev\PycharmProjects\ai\ReAct-Agent

  ▸ [Y] Разрешить
    [S] Разрешить в этой сессии — не спрашивать снова
    [N] Нет — отклонить
```

- `▸` — подсвеченный вариант (teal-«кнопка», как сейчас).
- Навигация **↑/↓** (заменяет нынешнюю ←/→), Enter подтверждает выделенный;
  `Y`/`S`/`N` — прямые шорткаты; `Esc` = Нет.
- Дефолтная подсветка — `[Y]`.

Семантика выбора:
- **[Y] Разрешить** — этот вызов. (Запоминается на прогон, см. anti-loop.)
- **[S] Разрешить в этой сессии** — «да» по этому ключу на весь тред (переживает
  прогоны внутри сессии).
- **[N] Нет** — отказ; запоминается **на прогон**; новый запрос пользователя
  спрашивает заново.

**Anti-loop:** любое решение кэшируется на прогон → агент не переспрашивает тот же
ключ в этой задаче. На **[N]** агент дополнительно получает инструктивный deny (§5);
если он продолжает дёргать тот же ключ — `confirm_port` сразу возвращает deny из
run-кэша **без баннера** (пользователя не долбит).

## 2. Три яруса решений (приоритет сверху вниз)

| Ярус | Что хранит | Где живёт | Срок жизни | Кто пишет |
|---|---|---|---|---|
| **config.yml** (manual) | allow/deny по путям/схемам (`extra_paths`, `allowed_*`) | `config.yml` | все треды | человек |
| **session** (`[S]`) | «да» по ключу | `state.shared["confirm_rules"]` → `meta_*.json` | жизнь треда | баннер `[S]` |
| **run** (авто) | любое решение по ключу | in-memory (`PermissionStore` на `ctx`) | один прогон | `confirm_port` |

- **config-ярус не трогаем**: существующие allowlist'ы отрабатывают **внутри гардов**
  до вызова `request_confirm` (как сейчас). Mid НЕ переносит их в новый движок — это Full.
- Mid добавляет **только** динамическую память (run + session) поверх.
- `confirm_port` проверяет session-ярус, затем run-ярус; нет матча → баннер.

## 3. Архитектура

### 3.1. Новый юнит — `PermissionStore` (`src/security/permission_store.py`)
Изолированный, тестируемый отдельно. Держит run-кэш + session-правила.

Интерфейс (примерно):
```
class PermissionStore:
    @classmethod
    def from_shared(cls, shared_rules: dict | None) -> "PermissionStore": ...
    def decision_for(self, key: str) -> bool | None:   # True=allow, False=deny, None=спросить
        # session-правило приоритетнее run-кэша; внутри яруса — точный матч ключа
    def record(self, key: str, allow: bool, *, session: bool) -> None:
        # всегда пишет в run-кэш; при session=True — ещё и в session-правила
    def session_rules(self) -> dict:   # для флаша в state.shared
```
- **Ключ**: `"<action>:<value>"`, где `action ∈ {write, read, url, command}`,
  `value` — путь/URL/команда. Namespace не даёт «да на запись» протечь в «чтение».
- **Матч (Mid)**: только точное равенство ключа. Глобы/префиксы — Full.

### 3.2. Жизненный цикл — на границе `AgentExecutor.run`
- **Старт прогона**: `store = PermissionStore.from_shared(state.shared.get("confirm_rules"))`;
  `ctx.permission_store = store`.
- **Флаш**: **сразу при добавлении session-правила** (`[S]`) — `state.shared["confirm_rules"]
  = store.session_rules()` — чтобы правило было durable даже при обрыве прогона. Дальше
  работает существующая персистентность (`shared` → `meta_*.json`, перечитывается в
  следующем прогоне той же сессии). Run-кэш (`[Y]`/`[N]`) в `shared` не флашится — он
  in-memory и умирает с прогоном.
- Так `confirm_port` трогает **только** `ctx.permission_store`; state-плезинг через гарды
  не нужен; единственная точка персистентности — executor.

### 3.3. `confirm_port.request_confirm` — единственная точка политики
```
def request_confirm(*, title, question, fields, ctx, decision_key: str | None = None) -> bool:
    store = getattr(ctx, "permission_store", None)
    if store is not None and decision_key is not None:
        cached = store.decision_for(decision_key)
        if cached is not None:
            return cached                      # уже решено (run/session) — без баннера
    if not confirm_available(ctx):
        result, scope = False, "run"           # headless/non-tty: fail-closed
    else:
        choice = _ask(ctx, title, question, fields)   # "once" | "session" | "deny"
        result = choice in ("once", "session")
        scope = "session" if choice == "session" else "run"
    if store is not None and decision_key is not None:
        store.record(decision_key, result, session=(scope == "session"))
    return result
```
- `_ask` — это TUI-баннер (`ctx.tui_confirm`) или CLI-stdin; оба теперь возвращают
  **3-значный выбор** `once|session|deny`, а не bool.

### 3.4. Confirm-канал становится 3-значным (расширение T1)
- TUI: `make_tui_confirm`/`submit_confirm_reply`/`_answer_confirm`/баннер несут
  `ConfirmChoice` (`once|session|deny`) вместо bool. `[S]` → `session`, `[Y]`/Enter(на Y) →
  `once`, `[N]`/`Esc` → `deny`.
- CLI: `prompt_user_confirmation` получает 3-значный вариант (`y/s/n`); headless по-прежнему
  fail-closed (нет интерактива → `deny`, `[S]` недоступен).

### 3.5. Гарды — минимальная правка
Каждый из 5 `ask`-сайтов передаёт `decision_key` (namespace+значение):
`write:<path>` (path_policy), `write:<dest>` (download_file), `read:<path>` (read_guard),
`url:<url>` (url_policy), `command:<payload>` (command_moderation + command_guard).
Больше в гардах ничего не меняется.

## 4. Поведение по режимам

- **TUI**: 3-кнопочный вертикальный баннер (§1).
- **CLI (tty)**: stdin-промпт с теми же 3 выборами (`[y]/[s]/[n]`); run+session работают.
- **Headless / CLI-non-tty / server**: баннера нет → `confirm_available=False` →
  **fail-closed deny**; run-кэш гасит повтор; `[S]` недоступен. Без регресса к текущему.
- **Swarm**: как headless (sandbox без tty) — fail-closed.

## 5. Инструктивный deny (шаблон)

При отклонении тул возвращает агенту (англ., как остальные error-строки):
> `User declined the write to '<path>'. Do NOT retry the same write, and do NOT silently
> write somewhere else — ask the user how to proceed, or stop and report that it was declined.`

По слоям меняется только глагол: read → `read a different file`; url → `fetch a different
URL`; command → `run a modified command`. Цель: глушит retry-loop **и** закрывает дыру
«сделал не туда без спроса». Текст — детерминированный (не зависит от модели).

## 6. Тестирование

- **`PermissionStore`** (юнит): `record/decision_for`; run vs session приоритет;
  namespace-изоляция (`write:` ≠ `read:`); точный матч; `from_shared`/`session_rules` round-trip.
- **`confirm_port`**: повтор ключа → без `_ask` (кэш); 3-значный выбор → правильный scope +
  bool; нет store / нет ключа → старое поведение (всегда спрашивать); fail-closed без tty.
- **Гарды** (на каждый): `[N]` → авто-deny на повтор в прогоне + инструктивное сообщение;
  `[S]` → следующий прогон не спрашивает (seed из `meta.shared`).
- **Персистентность**: session-правило долетает в `meta.shared` и перечитывается новым прогоном.
- **TUI**: вертикальный баннер, ↑/↓ навигация, 3 шортката, снапшот; re-entrancy (decline→approve).
- **Регресс**: CLI tty всё ещё промптит; headless fail-closed; 5 пред-существующих
  Windows-падений (symlink/case/path в `test_path_policy`/`test_read_guard`) — игнор, новых нет.

## 7. Вне scope (Full — отдельная спека)

- Режимы разрешений (`default/auto-accept/plan/bypass`) с тоглом (хоткей/флаг).
- Glob/prefix-матчинг ключей (`write:src/**`, `command:git *`, `url:domain:…`).
- Иерархия scope `project(.gc) > user(~/.gc) > built-in` + мерж.
- Унификация существующих config-allowlist'ов в общий движок правил.
- Файл `.gc/permissions.yml` и TUI `/permissions` (просмотр/отзыв правил).
- «Нет на всю сессию» 4-й кнопкой (`[D]`): сейчас «нет» = на прогон; durable-deny — через
  `config.yml` вручную.

## 8. Затрагиваемые файлы (ориентир для плана)

- Новый: `src/security/permission_store.py` (+ тест).
- `src/security/confirm_port.py` — `decision_key`, store-резолв, 3-значный `_ask`.
- `src/agent_executor.py` — seed/flush store на границе `run` (через `state.shared`).
- `src/security/path_policy.py`, `src/tools/exec/download_file.py`, `src/security/url_policy.py`,
  `src/security/read_guard.py`, `src/security/command_moderation.py` — передать `decision_key`
  + инструктивный deny (§5).
- TUI: `src/tui/widgets/confirm_banner.py` (вертикаль, 3 варианта, ↑/↓), `src/tui/screens/main.py`
  (`_answer_confirm`/перехват клавиш под 3 выбора), `src/tui/services/agent_runner.py`
  (`make_tui_confirm`/`submit_confirm_reply` → `ConfirmChoice`), `src/tui/app.py` (`_present_confirm`).
- `src/security/command_guard.py` — `decision_key` для command-tripwire ask.

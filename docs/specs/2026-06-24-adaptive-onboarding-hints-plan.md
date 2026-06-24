# Adaptive Onboarding Hints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать подсказку первого запуска (`onboarding.setup_guide`) стратегия-зависимой: показывать ровно незаполненные требования (endpoint / auth / model), где auth удовлетворяется токеном ИЛИ блоком `agent_auth`; если всё покрыто — позитивное «настроен». Зашить в дефолт только модель; quickstart-доку удалить.

**Architecture:** `onboarding.py` переходит с плоского списка `_Field(get, sentinel)` на требования-предикаты `_Requirement(is_satisfied(config) -> bool, hint)`. Предикаты читают плоский `config_dict` из `load_config`. Контроллеры (`cli.py`, `tui/__init__.py`) не меняются — `setup_guide` сам возвращает позитивное сообщение при пустом pending.

**Tech Stack:** Python ≥3.10, pytest. Конфиг — `config.default.yml` (YAML), HTML-инструкция — статический `docs/gc-ua-install.html`.

## Global Constraints

- Импорты — абсолютные от `src/` (`import onboarding`, `from config_modules import env_vars`). Без относительных, без `src.`-префикса.
- TDD: сначала падающий тест, потом минимальная реализация. Тесты — прямые `assert f(x) == y`, на `pytest`.
- Коммиты — лаконичные, на русском/смешанном языке окружения; без упоминаний AI/Claude/Anthropic, без `Co-Authored-By`.
- Не коммитить `.claude/`, `CLAUDE.md`.
- Сентинелы — единый источник истины в `onboarding.py`, сверяются тестом-стражем с `config.default.yml`.
- Реальный endpoint в репо НЕ коммитим (остаётся плейсхолдером). Бейким только модель `gigacode_agent_v1.6_rc`.
- Пуш: перед `git push` делать `git pull --rebase` (на ветке возможна параллельная работа).

## File Structure

- `src/onboarding.py` — **переписать** ядро: предикаты + `_Requirement` + `REQUIREMENTS` + `pending_requirements`/`is_unconfigured`/`setup_guide`. Одна ответственность: политика «настроен ли конфиг» + текст-гайд.
- `tests/test_onboarding.py` — **переписать**: добавить кейсы auth-«либо/либо», adaptive-model, позитивное сообщение, обновить страж.
- `config.default.yml` — **точечно**: бамп `agent.model`. (Header-комментарий уже перечисляет только endpoint+token — не трогаем.)
- `docs/install-quickstart-ru.md` — **удалить**.
- `docs/gc-ua-install.html` — **точечно**: убрать строку `model` из шага 4; убрать комментарий-источник на строку quickstart.
- `docs/2026-06-23-corp-mirror-config-overlay.md` — **точечно**: убрать ссылки на quickstart из actionable-чеклиста.

Ветка: `feat/agent-instructions-and-gc-guard` (текущая). Изоляция/worktree не требуется.

---

### Task 1: `onboarding.py` — стратегия-зависимые требования

**Files:**
- Modify (переписать): `src/onboarding.py`
- Test (переписать): `tests/test_onboarding.py`

**Interfaces:**
- Consumes: плоский `config_dict` из `config.load_config` — ключи `completions_url`, `chat_completion_headers` (dict с `Authorization`), `model`, `agent_auth` (dict, `{}` если не задан).
- Produces:
  - `URL_SENTINEL: str`, `TOKEN_SENTINEL: str`, `MODEL_SENTINEL: str`
  - `pending_requirements(config: dict) -> list[_Requirement]`
  - `is_unconfigured(config: dict) -> bool`
  - `setup_guide(config_path: str, config: dict) -> str`
  - `_Requirement` = `NamedTuple(is_satisfied: Callable[[dict], bool], hint: str)`

- [ ] **Step 1: Переписать тесты (RED)**

Заменить ВЕСЬ файл `tests/test_onboarding.py` на:

```python
"""Онбординг первого запуска: детект ненастроенного конфига + текст-гайд."""
import yaml

import config
import onboarding
from bundled_assets import bundled_root
from config_modules import env_vars


def _default_yaml() -> dict:
    return yaml.safe_load(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8")
    )


def _isolated_default(tmp_path, monkeypatch) -> dict:
    """config.default.yml реальным загрузчиком, изолировав машинный и project-local overlay."""
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)
    return config.load_config(str(bundled_root() / "config.default.yml"))


# --- базовый детект -------------------------------------------------------

def test_default_config_is_unconfigured(tmp_path, monkeypatch):
    cfg = _isolated_default(tmp_path, monkeypatch)
    assert onboarding.is_unconfigured(cfg) is True


def test_configured_when_url_token_model_set():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_unconfigured_if_token_still_placeholder():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.is_unconfigured(cfg) is True


# --- auth: токен ИЛИ agent_auth ------------------------------------------

def test_agent_auth_satisfies_auth_without_token():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {"auth_endpoint": "https://auth.local/token"},
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_agent_auth_set_drops_token_from_guide():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {"auth_endpoint": "https://auth.local/token"},
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
    }
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "Authorization" not in text
    assert "настроен" in text


def test_agent_auth_without_endpoint_does_not_satisfy_auth():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {},
    }
    assert onboarding.is_unconfigured(cfg) is True


# --- adaptive model ------------------------------------------------------

def test_model_sentinel_is_pending():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": onboarding.MODEL_SENTINEL,
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "agent.model" in text


def test_filled_model_not_in_guide():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "agent.model" not in text


# --- позитивное сообщение, когда всё покрыто -----------------------------

def test_setup_guide_positive_when_all_covered():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "настроен" in text
    assert "можно запускать" in text
    assert "•" not in text


# --- страж сентинелов vs config.default.yml ------------------------------

def test_sentinels_match_default_yaml():
    raw = _default_yaml()
    assert raw["backend"]["completions_url"] == onboarding.URL_SENTINEL
    assert (
        raw["backend"]["chat_completion_headers"]["Authorization"]
        == onboarding.TOKEN_SENTINEL
    )
    assert raw["agent"]["model"]
    assert raw["agent"]["model"] != onboarding.MODEL_SENTINEL


def test_setup_guide_lists_pending_fields_and_path(tmp_path, monkeypatch):
    cfg = _isolated_default(tmp_path, monkeypatch)
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "/home/u/.gc/config.yml" in text
    assert "backend.completions_url" in text
    assert "Authorization" in text
    assert "agent.model" not in text
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают (RED)**

Run: `uv run pytest tests/test_onboarding.py -q`
Expected: FAIL (новые имена `pending_requirements`/`MODEL_SENTINEL`/позитивное сообщение ещё не существуют; часть кейсов падает).

- [ ] **Step 3: Переписать `src/onboarding.py` (минимальная реализация)**

Заменить ВЕСЬ файл `src/onboarding.py` на:

```python
"""Онбординг первого запуска: политика «настроен ли конфиг» + текст-гайд.

Модуль-владелец презентации/политики первого запуска. Контроллеры (cli, tui)
зовут is_unconfigured()/setup_guide(); сюда же вынесены сентинелы дефолт-шаблона
(единый источник истины, сверяется тестом с config.default.yml).

Требования стратегия-зависимы: auth удовлетворяется токеном ИЛИ блоком agent_auth
(сертификатная авторизация — без статического токена). Подсказка перечисляет только
реально незаполненное; если всё покрыто — позитивное «настроен».
"""
from typing import Any, Callable, Dict, List, NamedTuple

# Сентинелы из config.default.yml (страж совпадения — tests/test_onboarding.py).
URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"
TOKEN_SENTINEL = "Bearer <ВПИШИТЕ_ТОКЕН>"
MODEL_SENTINEL = "<ВПИШИТЕ_МОДЕЛЬ>"


def _endpoint_set(c: Dict[str, Any]) -> bool:
    return c.get("completions_url") not in (None, URL_SENTINEL)


def _model_set(c: Dict[str, Any]) -> bool:
    return c.get("model") not in (None, "", MODEL_SENTINEL)


def _token_set(c: Dict[str, Any]) -> bool:
    auth = (c.get("chat_completion_headers") or {}).get("Authorization")
    return auth not in (None, "", TOKEN_SENTINEL)


def _agent_auth_set(c: Dict[str, Any]) -> bool:
    auth_cfg = c.get("agent_auth") or {}
    return bool(auth_cfg) and bool(auth_cfg.get("auth_endpoint"))


def _auth_set(c: Dict[str, Any]) -> bool:
    return _token_set(c) or _agent_auth_set(c)


class _Requirement(NamedTuple):
    is_satisfied: Callable[[Dict[str, Any]], bool]
    hint: str   # что вписать в config.yml, если требование не удовлетворено


# Требования к рабочему конфигу. auth — «либо/либо»: токен ИЛИ agent_auth.
# Чтобы добавить требование: дописать строку (+ при необходимости сентинел и
# плейсхолдер в config.default.yml — тест-страж поймает рассинхрон).
REQUIREMENTS: List[_Requirement] = [
    _Requirement(
        _endpoint_set,
        "backend.completions_url — endpoint OpenAI-совместимого провайдера (chat/completions)",
    ),
    _Requirement(
        _auth_set,
        "backend.chat_completion_headers.Authorization — токен доступа (Bearer …)",
    ),
    _Requirement(
        _model_set,
        "agent.model — имя модели",
    ),
]


def pending_requirements(config: Dict[str, Any]) -> List[_Requirement]:
    """Требования, ещё не удовлетворённые текущим конфигом."""
    return [r for r in REQUIREMENTS if not r.is_satisfied(config)]


def is_unconfigured(config: Dict[str, Any]) -> bool:
    """True, если конфиг ещё не готов к запуску (есть незакрытое требование)."""
    return bool(pending_requirements(config))


def setup_guide(config_path: str, config: Dict[str, Any]) -> str:
    """Подсказка по config_path.

    pending непуст → что вписать. pending пуст → позитивное «настроен»."""
    pending = pending_requirements(config)
    if not pending:
        return f"[gc] Конфиг настроен: {config_path} — можно запускать."
    lines = "\n".join(f"  • {r.hint}" for r in pending)
    return (
        f"[gc] Конфиг ещё не настроен: {config_path}\n"
        f"     Впишите перед запуском:\n{lines}\n"
        f"     затем запустите снова."
    )
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят (GREEN)**

Run: `uv run pytest tests/test_onboarding.py -q`
Expected: PASS (все кейсы).

- [ ] **Step 5: Прогнать смежные — cli/tui онбординг не сломан**

Run: `uv run pytest tests/ -q -k "onboarding or init_config or init_command"`
Expected: PASS. Если падает что-то, ассертившее старый текст подсказки/`unconfigured_fields` — обновить под новый API (`pending_requirements`) / новый текст.

- [ ] **Step 6: Commit**

```bash
git add src/onboarding.py tests/test_onboarding.py
git commit -m "feat(onboarding): стратегия-зависимая подсказка — auth=токен|agent_auth, adaptive model, позитив при покрытом конфиге"
```

---

### Task 2: `config.default.yml` — бамп модели

**Files:**
- Modify: `config.default.yml` (строка с `agent.model`)

**Interfaces:**
- Consumes: страж `test_sentinels_match_default_yaml` (Task 1) — требует `agent.model` непустой и `!= MODEL_SENTINEL`.
- Produces: дефолт-модель `gigacode_agent_v1.6_rc`.

- [ ] **Step 1: Заменить значение модели**

В `config.default.yml`, секция `agent:`:

```yaml
  model: "gigacode_agent_v1.6_rc"
```
(было `"gigacode_agent_v1_rc"`). Header-комментарий (строки 7-9: «впишите 1) completions_url 2) Authorization») НЕ трогаем — он уже перечисляет только endpoint+token, модель preset.

- [ ] **Step 2: Прогнать страж**

Run: `uv run pytest tests/test_onboarding.py::test_sentinels_match_default_yaml -q`
Expected: PASS (модель задана, `!= MODEL_SENTINEL`).

- [ ] **Step 3: Commit**

```bash
git add config.default.yml
git commit -m "chore(config): дефолт-модель gigacode_agent_v1.6_rc"
```

---

### Task 3: Доки — удалить quickstart, поправить HTML и ссылки

**Files:**
- Delete: `docs/install-quickstart-ru.md`
- Modify: `docs/gc-ua-install.html` (шаг 4 — строка `model`; head-комментарий-источник)
- Modify: `docs/2026-06-23-corp-mirror-config-overlay.md` (actionable-чеклист)

**Interfaces:**
- Consumes: —
- Produces: HTML-инструкция без шага правки модели и без ссылок на удалённый quickstart.

- [ ] **Step 1: Удалить quickstart**

```bash
git rm docs/install-quickstart-ru.md
```

- [ ] **Step 2: HTML — убрать строку `model` из шага 4**

В `docs/gc-ua-install.html` найти блок шага 4 (около строки 138-145, `<pre>`/`<div>` с правкой config.yml). Удалить строку с `model` (она помечена `# если выдали`):

```
  <span class="tok-key">model</span>: <span class="tok-str">"имя_модели"</span>                  <span class="tok-cmt"># если выдали</span>
```

Оставить только `completions_url` и `Authorization`. Если в строке выше (`Authorization`) есть хвостовой перенос/закрывающий тег, проверить, что блок остаётся валидным (закрывающий `</div>`/`</pre>` на месте).

- [ ] **Step 3: HTML — убрать head-комментарий-источник про quickstart**

В `docs/gc-ua-install.html` (около строки 5) удалить строку-комментарий вида:

```
  Контент — источник истины: docs/install-quickstart-ru.md
```

Если рядом есть формулировка «синхронно с install-quickstart-ru.md» — переписать на «страница самодостаточна» или удалить.

- [ ] **Step 4: corp-mirror note — убрать ссылки на quickstart**

В `docs/2026-06-23-corp-mirror-config-overlay.md`:
- строка ~83: убрать упоминание `docs/install-quickstart-ru.md` (оставить только `docs/gc-ua-install.html`);
- строка ~131 (чеклист): аналогично — пункт про install-URL оставить только для HTML.

- [ ] **Step 5: Визуальная проверка HTML**

Открыть `docs/gc-ua-install.html` в браузере (или `start docs/gc-ua-install.html`). Убедиться: шаг 4 показывает правку только `completions_url` + `Authorization`; вёрстка не поехала; ссылок на quickstart нет.

- [ ] **Step 6: Commit**

```bash
git add -A docs/
git commit -m "docs: удалить install-quickstart (дублирует HTML); шаг конфига — endpoint+token (модель preset)"
```

---

## Self-Review

**1. Spec coverage:**
- Стратегия-зависимые требования (предикаты, auth=токен|agent_auth) → Task 1. ✓
- Бейким только модель; endpoint/token плейсхолдеры → Task 2 (модель) + Task 1 (endpoint/token остаются сентинелами). ✓
- `setup_guide` без фолбэка + позитив при покрытом → Task 1 (Step 3 реализация, Step 1 тест `test_setup_guide_positive_when_all_covered`). ✓
- Контроллеры не трогаем → подтверждено (Task 1 не меняет cli/tui; Step 5 проверяет, что онбординг-тесты cli/tui зелёные). ✓
- auth-hint только про токен → текст требования auth = «Authorization — токен доступа». ✓
- Удалить quickstart + правка HTML + demo без изменений → Task 3 (demo не в списке файлов). ✓
- Честная оговорка про model (всегда непустая) → отражена тестами `test_filled_model_not_in_guide` + `test_model_sentinel_is_pending` (адаптив виден только на сентинеле). ✓

**2. Placeholder scan:** Код в Task 1/Task 2/Task 3 приведён полностью; команды и ожидаемый результат указаны. HTML-правка Task 3 Step 2/3 даёт точные строки для удаления (взяты из текущего файла). ✓

**3. Type consistency:** `_Requirement(is_satisfied, hint)` — единое имя во всех тестах и реализации. `pending_requirements`/`is_unconfigured`/`setup_guide` совпадают между Task 1 Interfaces, тестами и реализацией. Сентинелы `URL_SENTINEL`/`TOKEN_SENTINEL`/`MODEL_SENTINEL` — одинаковы в коде и тестах. ✓

**Открытые (вне scope, флаг пользователю):** ссылки на quickstart остаются в исторических доках `docs/specs/2026-06-21-install-landing-page-design.md` и `docs/CHANGES-vs-master.md` (строка-фича + открытый пункт). Это датированные записи — предлагаю оставить как есть либо обновить отдельно по желанию.

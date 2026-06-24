# Адаптивные подсказки онбординга (стратегия-зависимый setup_guide)

Дата: 2026-06-24
Статус: дизайн утверждён, ждёт ревью спеки → план

## Проблема

При первом запуске агент печатает подсказку «что вписать в конфиг» (`onboarding.setup_guide`).
Сейчас она:

- знает только два поля — `completions_url` (endpoint) и `Authorization` (token) — через
  плоский список `REQUIRED_FIELDS` (`_Field(get, sentinel, …)`), «не настроено» = значение
  равно плейсхолдеру;
- модель не учитывает адаптивно — всегда добавляет статическую приписку
  `(проверьте также agent.model)`, даже если модель уже задана;
- не знает про сертификатную авторизацию (`agent_auth`): в корп-пути токен не нужен
  (авторизация по сертификатам машины + auth-endpoint, `use_ssl: true`), но подсказка
  всё равно потребовала бы токен — сбивая с толку.

Цель: подсказка составляется «грамотными проверками» — показывает ровно то, чего реально
не хватает, и **не показывается вообще**, если всё покрыто.

## Решения (из брейншторма)

1. В `config.default.yml` мы **зашиваем только модель** (`gigacode_agent_v1.6_rc`).
   `completions_url` и `Authorization` остаются плейсхолдерами — пользователь вписывает их сам.
   Реальный endpoint в upstream-репо не коммитим (он живёт в корп-зеркале — вне scope).
2. `onboarding.py` обобщается на **стратегия-зависимые требования** (подход A: предикаты).
3. auth — требование «либо/либо»: удовлетворяется токеном **ИЛИ** настроенным `agent_auth`.
4. Текст подсказки про auth — **только про токен** (в корп-пути `agent_auth` покрывает auth,
   поэтому подсказка про auth там не показывается вовсе; «или agent_auth» не упоминаем).
5. `docs/install-quickstart-ru.md` — **удаляем** (дублирует HTML-инструкцию).
6. `demo_config.yml` — без изменений (документированный пример с плейсхолдерами).

## Дизайн

### `src/onboarding.py` — требования-предикаты

Заменяем плоский `_Field`/`REQUIRED_FIELDS` на требования с предикатом готовности.

Константы-сентинелы (источник истины, сверяется тестом с `config.default.yml`):

- `URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"` (как есть)
- `TOKEN_SENTINEL = "Bearer <ВПИШИТЕ_ТОКЕН>"` (как есть)
- `MODEL_SENTINEL = "<ВПИШИТЕ_МОДЕЛЬ>"` (новая; в наш дефолт не шипается — модель задана реально,
  сентинел нужен механизму как «как выглядит незаполненная модель» для более «постных» конфигов)

Хелперы-предикаты (читают плоский `config_dict` из `load_config`):

- `_endpoint_set(c)`: `c.get("completions_url") not in (None, URL_SENTINEL)`
- `_model_set(c)`: `c.get("model") not in (None, "", MODEL_SENTINEL)`
- `_token_set(c)`: `(c.get("chat_completion_headers") or {}).get("Authorization") not in (None, "", TOKEN_SENTINEL)`
- `_agent_auth_set(c)`: `bool(c.get("agent_auth")) and bool(c["agent_auth"].get("auth_endpoint"))`
- `_auth_set(c)`: `_token_set(c) or _agent_auth_set(c)`

Требование:

```python
class _Requirement(NamedTuple):
    is_satisfied: Callable[[Dict[str, Any]], bool]
    hint: str   # человекочитаемо: что вписать, если не satisfied

REQUIREMENTS = [
    _Requirement(_endpoint_set, "backend.completions_url — endpoint OpenAI-совместимого провайдера (chat/completions)"),
    _Requirement(_auth_set,     "backend.chat_completion_headers.Authorization — токен доступа (Bearer …)"),
    _Requirement(_model_set,    "agent.model — имя модели"),
]
```

Публичный API (сигнатуры сохраняются — `cli.py`/`tui` не трогаем):

- `pending_requirements(config) -> list[_Requirement]` (переименование `unconfigured_fields`)
- `is_unconfigured(config) -> bool` = `bool(pending_requirements(config))`
- `setup_guide(config_path, config) -> str`:
  - pending непуст → «Конфиг ещё не настроен: …\nВпишите: …» (перечисляет `hint` только из pending);
  - pending пуст → **позитивное** «Конфиг настроен: {path} — можно запускать.».
  - Текущий фолбэк `pending = unconfigured_fields(config) or list(REQUIRED_FIELDS)` **удаляем** —
    он маскировал «всё покрыто», показывая все поля. Статическую приписку про `agent.model`
    тоже удаляем (теперь это адаптивное требование).

**Контроллеры не меняем.** Есть два класса вызовов, оба остаются корректными сами собой:

- `--init-config` (`cli._run_init:118`, `tui.__init__:85`) — зовут `setup_guide` безусловно после
  создания конфига. Раньше при покрытом конфиге (напр. `--init-config` из корп-зеркала) фолбэк
  врал «впишите endpoint/token»; теперь `setup_guide` сам вернёт позитивное «настроен, можно
  запускать». Правки контроллеров не нужны.
- run-путь (`cli.main:173`, `tui.__init__:95`) — уже гейтят через `is_unconfigured`, печатают гайд
  только при pending. Без изменений.

### Поведение по сценариям

| Конфиг | pending | Подсказка |
|---|---|---|
| Наш дефолт (endpoint+token плейсхолдеры, model задана, agent_auth нет) | {endpoint, auth} | endpoint + токен |
| Корп (endpoint+model+agent_auth заданы, токена нет) | {} | **не показывается** |
| Заполнено всё, кроме токена (token-путь) | {auth} | токен |
| agent_auth задан, но endpoint — плейсхолдер | {endpoint} | endpoint (про токен молчим) |

**Про model честно:** `agent.model` всегда непустая (наш дефолт задаёт `gigacode_agent_v1.6_rc`,
иначе pydantic-fallback `"MiniMax-M2.7"`), поэтому `_model_set` практически всегда True.
Практический эффект требования model — убрать вечную приписку `(проверьте также agent.model)` и
сделать логику единообразной; адаптивность реально сработает лишь для конфига, который явно
шипает `MODEL_SENTINEL`.

### `config.default.yml`

- `agent.model: gigacode_agent_v1.6_rc` (было `gigacode_agent_v1_rc`).
- `completions_url`, `Authorization` — плейсхолдеры без изменений; `use_ssl: false` без изменений.
- Шапка-комментарий «ПЕРЕД ПЕРВЫМ ЗАПРОСОМ впишите: 1) endpoint 2) token» — корректируем:
  модель уже задана, вписать нужно endpoint + token.

### Документация

- Удалить `docs/install-quickstart-ru.md` и ссылки на него (если есть в HTML/доках).
- `docs/gc-ua-install.html`: шаг редактирования конфига — вписать **endpoint + token**
  (модель preset, из ручных шагов убрать). Ссылку на quickstart убрать.
- `demo_config.yml` — без изменений.

## Тесты (TDD)

`tests/test_onboarding.py` — переписать страж и добавить кейсы:

- наш дефолт → `pending == {endpoint, auth}`; модель не в подсказке;
- `agent_auth` настроен (с `auth_endpoint`) → токен **не** в подсказке;
- всё покрыто (corp-кейс: endpoint+model+agent_auth, токена нет) → `is_unconfigured() is False`;
  `setup_guide` на таком конфиге → позитивное «Конфиг настроен …» (без перечня полей);
- `model == MODEL_SENTINEL` → model попадает в подсказку (адаптивность);
- страж сентинелов: в `config.default.yml` `completions_url == URL_SENTINEL`,
  `Authorization == TOKEN_SENTINEL`, `agent.model` задана и `!= MODEL_SENTINEL`.

Тесты гермитичны — реальный endpoint не используется.

## Граничные случаи

- `agent_auth` присутствует, но без `auth_endpoint` → `_agent_auth_set` = False → auth не покрыт
  → подсказка про токен (корректно: коннектор бы не построился).
- Пустая строка в `Authorization`/`model` → трактуется как незаполнено.
- `--init-config` на полностью покрытом конфиге → `setup_guide` вернёт позитивное «настроен»,
  а не ложный перечень полей (см. секцию про контроллеры). Правки контроллеров не требуется.

## Вне scope

- Корп-зеркало (реальные endpoint/model/`agent_auth` в его `config.default.yml`) — отдельная ветка
  в корп-среде; здесь только механизм + наш upstream-дефолт (модель).
- Изменение схемы `agent_auth` — не трогаем; используем существующий `build_auth_connector`-контракт
  («agent_auth непустой и есть auth_endpoint»).

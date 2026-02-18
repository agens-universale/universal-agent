# Список правок для консистентного CLI с уменьшенным функционалом

После сокращения конфига и удаления модуля `tools/tasks` часть функционала стала недоступной или опирается на отсутствующие параметры. Ниже — актуальный статус: что уже сделано и что при желании можно доработать.

---

## Статус: что уже исправлено

- **Локальный системный промпт** — в `process_user_ask` ветка с `state.system_prompt` убрана, отправляется только пользовательское сообщение (`messages_to_send = [message]`). **Файл:** `src/processing_responses.py`.
- **Импорт `render_system_prompt`** — удалён из `src/cli.py`.
- **Модуль `local_render_system_prompt.py`** — удалён из проекта.
- **Конфиг** — в `config.yml` добавлен `server_tool_prefix: system`, при `use_server_subagents: true` KeyError больше не возникает. Параметр `use_chat_completions_only` из конфига убран.
- **Модуль `tools/tasks`** — удалён, ссылок в коде нет.

---

## 1. Конфиг и его использование

### 1.1 Параметры в конфиге, не используемые в коде

В коде не читаются: `completion_tool`, `blocking_tools`, `no_tool_call_max_count`, `backend_retry_count`, `backend_retry_delay_sec`, `backend_unavailable_message`, `mcp_retry_count`, `mcp_retry_delay_sec`.  
Они есть в `config.yml` — либо оставить как зарезервированные (с комментарием), либо удалить из конфига.

**Файл:** `config.yml`.

### 1.2 Использование конфига в коде

- `server_tool_prefix` — добавлен в конфиг, в `src/tools/__init__.py` обращение к нему безопасно при наличии секции.
- Для большей устойчивости при упрощении конфига в `apply_tool` можно читать префикс через `config.config_dict.get('server_tool_prefix', '')`.

**Файл:** `src/tools/__init__.py` (опционально).

---

## 2. State: parent_state_id и метаданные

### 2.1 `src/states.py` — класс `State`

- В `__init__` по-прежнему есть параметры `parent_state_id`, `mode`, `call_id`, `specific_role`. Они не используются в упрощённом сценарии; можно оставить как есть (передаётся `None` из CLI) или со временем убрать из конструктора и из `to_dict`.
- В `set_call_metadata`:
  - `config.config_dict['system_prompt']['system_prompt_id']` — при пустой секции `system_prompt` в конфиге (после `config.py`: `config_dict['system_prompt'] = dict()`) будет `KeyError`. Рекомендация: читать через `.get`, например `config.config_dict.get('system_prompt', {}).get('system_prompt_id', 'v1')`.
  - `config.config_dict.get('use_server_subagents', False)` — уже безопасно.

**Файлы:** `src/states.py`, при желании `src/config.py` (задать дефолт для `system_prompt_id` или структуры `system_prompt`).

### 2.2 `src/cli.py` — создание состояния

- В `state_data` передаётся `'parent_state_id': None`. Если уберёте `parent_state_id` из `State`, здесь его тоже убрать.

**Файл:** `src/cli.py`.

---

## 3. Конфиг: значения по умолчанию

### 3.1 `src/config.py`

- При отсутствии секции `system_prompt` подставляется пустой словарь. В `states.py` затем идёт обращение к `config_dict['system_prompt']['system_prompt_id']` → при пустом словаре возможен `KeyError`.  
  Варианты: в `config.py` задать `config_dict['system_prompt'] = {'system_prompt_id': 'v1'}` при отсутствии секции или в `states.set_call_metadata` читать через `.get('system_prompt', {}).get('system_prompt_id', 'v1')`.

**Файлы:** `src/config.py`, `src/states.py`.

---

## 4. Вывод «system» в логах

В `src/utils/format_messages.py` для сообщений с `role == 'system'` выводится заглушка «&lt;local system prompt. See debug logs&gt;» (строки 13–14 в `format_type_message` — там опечатка: проверяется строка `'role' == 'system'`, а не `role == 'system'`; строка 65 в `format_others_responses`).  
Локального системного промпта нет; при получении system-сообщений от бэкенда заглушка по-прежнему уместна. При желании можно упростить текст или оставить как есть.

**Файл:** `src/utils/format_messages.py` (при желании поправить опечатку в `format_type_message`).

---

## 5. Вызов инструментов: parent_state и run_agent_func

- В `process_tool_calls` по-прежнему передаётся `apply_tool(..., parent_state=state, run_agent_func=run_agent)`. Инструменты получают эти аргументы через `**kwargs`; в текущих simple_tools они не используются. Оставить как есть безопасно; при дальнейшем упрощении можно перестать передавать эти параметры и убрать их из сигнатуры.
- В `update_plan.py` и `reflect.py` в блоках `if __name__ == '__main__'` в kwargs по-прежнему передаются `run_agent_func` и `parent_state` для тестов — на работу основного потока не влияет.

**Файлы:** `src/processing_responses.py`, `src/tools/simple_tools/update_plan.py`, `src/tools/simple_tools/reflect.py` (опционально).

---

## 6. Сводка: параметры конфига и код

| Параметр в config | Где используется | Примечание |
|-------------------|------------------|------------|
| `agent_url` | `connect_backend.request_backend_responses` | используется |
| `completions_url` | `connect_backend.request_backend_chat_completions` | не в основном потоке |
| `model` | `cli.py` (state), `connect_backend.create_call_data_completions` | используется |
| `system_prompt.system_prompt_id` | `states.State.set_call_metadata` | при пустом `system_prompt` — риск KeyError |
| `system_prompt.use_server_subagents` | `states.State.set_call_metadata`, `tools.__init__.apply_tool` | используется, по умолчанию false |
| `server_tool_prefix` | `tools.__init__.apply_tool` | добавлен в конфиг |
| `client_name` | не найден в коде | — |
| `x_cl_id` | `connect_backend.create_call_headers` | используется |
| `verbose` | `cli.py` (state_data), аргумент CLI | используется |
| `max_steps` | `cli.py` (state_data) | используется |
| `use_ask_human_tool` | `tools.simple_tools.__init__` | используется |
| `dump_storage`, `save_logs` | `cli.py`, логирование | используется |
| `use_tool_reasons` | `tools.__init__.py` | используется (по умолчанию false) |
| `completion_tool`, `blocking_tools`, `no_tool_call_max_count`, retry-параметры | — | в коде не читаются |

---

## 7. Рекомендуемые опциональные доработки

1. **Устойчивость к упрощённому конфигу** — в `states.set_call_metadata` читать `system_prompt_id` через `.get('system_prompt', {}).get('system_prompt_id', 'v1')` (или задать дефолт в `config.py` при пустой секции `system_prompt`).
2. **config.yml** — удалить или явно пометить в комментариях неиспользуемые ключи (`blocking_tools`, `completion_tool`, retry, `no_tool_call_max_count`).
3. **format_messages.py** — при желании исправить проверку в `format_type_message`: сравнивать `role == 'system'`, а не `'role' == 'system'`.

После этих опциональных правок приложение остаётся консистентным с уменьшенным конфигом: без локального системного промпта, с поддержкой `server_tool_prefix` и без зависимости от удалённых модулей `tools/tasks` и `local_render_system_prompt`.

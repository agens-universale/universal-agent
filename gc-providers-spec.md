# Подключение LLM-провайдеров через LiteLLM Proxy

## Цель

Зафиксировать архитектуру подключения нескольких LLM-провайдеров к gc-cli без размещения провайдер-специфичного кода внутри проекта. Адаптация под конкретного провайдера (например, особенности валидаторов GigaChat или ограничения Yandex) делается **снаружи** — в LiteLLM Proxy и его конфигурации. Агент работает только с базовым форматом OpenAI Chat Completions.

Прямой путь на OpenAI (`api.openai.com`) или любой strict-OAI-совместимый эндпоинт работает без прокси через флаг `openai_strict_tools_schema` и `chat_completion_kwargs` / `chat_completion_headers`. LiteLLM Proxy подключается поверх для случаев когда апстрим не является чистым OpenAI strict — российские провайдеры (GigaChat, Yandex Cloud), локальные модели с особенностями (Ollama qwen3 thinking-mode), любые экзотические OAI-совместимые сервисы со своими преобразованиями.

## Принципы

1. **Имена провайдеров не упоминаются в `src/`.** Никаких `if model == "gigachat"` внутри агента.
2. **Провайдер-специфичные настройки задаются пользователем** в конфиге (агентском или прокси), а не в коде.
3. **Особенности API провайдеров** (требование непустого `content`, требование `properties` у объектов в схеме инструментов и т.п.) выносятся либо в LiteLLM Proxy, либо в конфигурацию пользователя.
4. **Готовое решение под преднастроенных провайдеров** поставляется как «батарейка» — отдельная подпапка `litellm/` в репозитории с заранее настроенным прокси.
5. **Свой экзотический провайдер** пользователь подключает рядом с прокси, не трогая код агента (`src/`):
   - в простом случае (OpenAI-совместимый апстрим, возможно с дополнительными полями) — одной записью в `litellm/proxy_config.yaml`;
   - в сложном случае (требуется структурное преобразование тела) — той же записью плюс собственный колбэк в `litellm/callbacks/_<name>.py`, который подключается в `litellm/callbacks/provider_normalizer.py` (dispatcher).

   В обоих случаях изменения изолированы в подпапке `litellm/`. Код агента не правится.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│ AGENT (gc-cli, src/)                                            │
│  Шлёт минимальный OpenAI Chat Completions payload:              │
│    model, messages, tools, tool_choice, max_tokens              │
│  + опциональные пользовательские поля из chat_completion_kwargs │
└─────────────────────────────────────────────────────────────────┘
                             │
                             │ POST /v1/chat/completions
                             │ Authorization: Bearer <master_key>
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ LiteLLM Proxy (litellm/)                                        │
│  • Маршрутизация по alias (gigachat-max, yandex-pro, …)         │
│  • Преобразование тела запроса под выбранного провайдера        │
│  • Подмешивание провайдерных полей (extra_body)                 │
│  • Очистка несовместимых полей (drop_params)                    │
│  • Авторизация в апстриме (OAuth, API-key, IAM-token)           │
└─────────────────────────────────────────────────────────────────┘
                             │
                             │ Native HTTPS
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ UPSTREAM API                                                    │
│  GigaChat │ Yandex Cloud │ OpenRouter │ Ollama │ кастомный …    │
└─────────────────────────────────────────────────────────────────┘
```

## Как работает LiteLLM Proxy

LiteLLM Proxy — это HTTP-сервис, который принимает запросы в формате OpenAI Chat Completions и переадресует их в апстрим-провайдер. Между приёмом и отправкой он применяет несколько механизмов преобразования. Ниже — те, которые мы используем.

### Список моделей (`model_list`)

Список именованных моделей, каждая с настройками апстрима. Имя (`model_name`) — это alias, который агент указывает в поле `model` своего запроса.

```yaml
model_list:
  - model_name: gigachat-max                  # alias, который агент ставит в поле model
    litellm_params:
      model: gigachat/GigaChat-2-Max          # фактическая цель: провайдер + модель
      api_key: "os.environ/GIGACHAT_CREDENTIALS"
      ssl_verify: false
```

При получении запроса прокси находит запись по `model_name`, берёт из неё `litellm_params` и формирует исходящий вызов.

### Подмешивание полей (`extra_body`)

Если апстрим-провайдер требует или принимает дополнительные поля поверх стандарта OpenAI (например, `top_k`, `repetition_penalty`, `chat_template_kwargs`), они описываются в `extra_body` записи модели:

```yaml
- model_name: my-vllm-finetune
  litellm_params:
    model: openai/my-finetune
    api_base: http://internal.host:8000/v1
    api_key: "os.environ/MY_VLLM_KEY"
    extra_body:
      top_k: 20
      min_p: 0.0
      repetition_penalty: 1.2
      chat_template_kwargs:
        enable_thinking: false
```

Поля из `extra_body` мерджатся в исходящий payload. Источник правды для всех пользователей этой модели — одно место в конфиге прокси.

### Очистка несовместимых полей (`drop_params`)

```yaml
litellm_settings:
  drop_params: true
```

Когда включено, прокси проверяет каждое поле исходящего запроса по таблице возможностей провайдера и **удаляет** поля, которые провайдер не поддерживает. Например, `top_k` будет принят кастомным vLLM-эндпоинтом, но автоматически вырежется перед отправкой в OpenAI (тот его не принимает и вернёт 400).

### Колбэки до и после вызова апстрима

Колбэк — это Python-класс, унаследованный от `CustomLogger`. Прокси вызывает его методы вокруг каждого исходящего запроса:

- `async_pre_call_hook(user_api_key_dict, cache, data, call_type)` — перед отправкой в апстрим. Принимает словарь `data` с готовым payload (`model`, `messages`, `tools`, ...), может **мутировать** его.
- `async_post_call_success_hook(data, user_api_key_dict, response)` — после получения ответа. Принимает оба `data` (запрос) и `response` (ответ модели), может мутировать `response` перед возвратом клиенту.

```python
class ProviderNormalizer(CustomLogger):
    async def async_pre_call_hook(
        self, user_api_key_dict, cache, data, call_type
    ):
        model = (data.get("model") or "").lower()

        if model.startswith("gigachat"):
            # 5-walker pipeline: properties:{} + type-on-field +
            # additionalProperties bool + strip unsupported keywords +
            # strip strict flag. См. раздел «Преобразования под GigaChat».
            _normalize_gigachat_tools(data.get("tools") or [])
            _backfill_empty_content(data.get("messages") or [])

        if model.startswith("yandex"):
            # Yandex Cloud отказывается обслуживать сообщения с пустым content.
            _backfill_empty_content(data.get("messages") or [])

        return data

    async def async_post_call_success_hook(
        self, data, user_api_key_dict, response
    ):
        model = (data.get("model") or "").lower()

        if model.startswith("yandex"):
            # Yandex отвечает JSON-в-content вместо нативных tool_calls.
            # Парсим, матчим с tools[*].function.parameters, синтезируем
            # OAI tool_calls structure. См. раздел «Преобразования под Yandex».
            _synthesize_tool_calls_for_yandex(response, data.get("tools") or [])

        return response

proxy_handler_instance = ProviderNormalizer()
```

Регистрация:

```yaml
litellm_settings:
  callbacks: callbacks.provider_normalizer.proxy_handler_instance
```

Колбэки — это **то самое место**, где живёт знание об особенностях конкретных провайдеров. Не в коде агента, а в файле под `litellm/callbacks/`. Алгоритмы в нём универсальные (`_ensure_object_properties` рекурсивно обходит JSON Schema; `_backfill_empty_content` подменяет пустой `content` на пробел) — диспетчеризация по имени провайдера происходит только тут.

### Подстановка значений из `.env` (`__VAR__` placeholders)

`litellm/proxy_config.yaml` хранится в git и не должен содержать PII (account-specific identifiers, например Yandex `folder_id`). Такие значения объявляются как `__VAR__` placeholder'ы:

```yaml
- model_name: yandex-gpt-pro
  litellm_params:
    model: openai/gpt://__YANDEX_FOLDER_ID__/yandexgpt/latest
    api_base: https://llm.api.cloud.yandex.net/v1
    api_key: os.environ/YANDEX_API_KEY
```

При запуске `litellm/run.py`:

1. Читает `litellm/.env` → `os.environ`.
2. Сканирует `proxy_config.yaml` на `__VAR__` placeholder'ы.
3. Подставляет значения из env → пишет результат в `litellm/proxy_config.local.yaml` (gitignored).
4. Запускает `litellm --config proxy_config.local.yaml --port <PORT>`.

Если хотя бы один placeholder отсутствует в env — `run.py` выходит с ошибкой и понятным сообщением, перечисляющим какие переменные нужно положить в `.env`.

Механизм generic: любой `__FOO_BAR__` в `proxy_config.yaml` подставляется из соответствующей env переменной. Под `os.environ/X`-синтаксис LiteLLM остаётся вторичной развилкой (он работает только для отдельных полей вроде `api_key`); placeholder'ы дают единый способ для любых строковых подстановок, включая внутри `model:`.

## Раскладка `litellm/`

LiteLLM как библиотека — **не часть исходного кода и не объявленная зависимость в `pyproject.toml`**. Версия пиннется внутри `litellm/install.py` (константа `LITELLM_PIN = "litellm[proxy]==1.83.14"`), и сам `install.py` делает `pip install` в текущий интерпретатор. Это зеркалит паттерн `recommended_mcp/install.py` (он тоже ставит wheels сам) и не вынуждает тянуть ~200 МБ транзитивных deps LiteLLM (uvicorn / fastapi / orjson / …) тем, кто прокси не использует. В репо лежит только тонкая «батарейка» — конфиг + наши hooks + два кросс-платформенных Python-скрипта.

```
litellm/
├── proxy_config.yaml          ← под git, канонический набор моделей + регистрация callback
│                                Содержит __VAR__ placeholder'ы для PII (folder_id и т.п.)
├── proxy_config.local.yaml    ← gitignored, генерируется `run.py` (placeholder'ы из .env)
├── callbacks/
│   ├── __init__.py
│   ├── provider_normalizer.py ← под git, dispatcher: класс + proxy_handler_instance
│   ├── _common.py             ← под git, vendor-agnostic helpers (JSON parse, tool_call shape, …)
│   ├── _gigachat.py           ← под git, 9-walker pre-call + 3-level post-call synth
│   └── _yandex.py             ← под git, backfill empty content + JSON-in-content synth
├── install.py                 ← под git, кросс-платформенный installer (pip install + .env seed)
├── run.py                     ← под git, кросс-платформенный launcher (render config + subprocess litellm)
├── README.md                  ← под git, quick-start
├── .env.example               ← под git, шаблон credentials и идентификаторов
├── .env                       ← gitignored, реальные credentials
└── .gitignore
```

Принципы (см. `principles.md`):

- **`src/` ↔ `litellm/` независимы.** Агент знает только URL прокси (`backend.completions_url`). Никаких импортов `litellm/...` из `src/` и наоборот.
- **Низкое зацепление между процессами.** Прокси — отдельный foreground-процесс, агент — отдельный. Каждый живёт независимо.
- **Без Docker (пока).** Пакуем только pip-зависимостью + двумя py-скриптами. Контейнеризация — отдельная задача в будущем.

## Список преднастроенных провайдеров (поставляются «из коробки»)

| Alias | Апстрим | Аутентификация | Где получить ключ |
|---|---|---|---|
| `gigachat-2-lite` | GigaChat 2 (Lite v2, free Freemium) | OAuth, `GIGACHAT_CREDENTIALS` | https://developers.sber.ru/studio/ |
| `gigachat-2-pro` | GigaChat 2 Pro | OAuth, `GIGACHAT_CREDENTIALS` | https://developers.sber.ru/studio/ |
| `gigachat-2-max` | GigaChat 2 Max | OAuth, `GIGACHAT_CREDENTIALS` | https://developers.sber.ru/studio/ |
| `yandex-gpt-lite` | YandexGPT Lite (`yandexgpt-lite/latest`) | API-key в `YANDEX_API_KEY` | console.cloud.yandex.ru |
| `yandex-gpt-pro` | **YandexGPT 5 Pro** (`yandexgpt/latest`, flagship) | API-key в `YANDEX_API_KEY` | console.cloud.yandex.ru |

Поле `model` в агентском `config.yml` принимает любой из этих alias. Переключение между провайдерами — смена одного значения.

## Преобразования тела запроса под GigaChat

GigaChat — это случай, в котором задействованы все четыре механизма прокси одновременно:

1. **Маршрутизация** — alias `gigachat-max` направляет запрос в нативный GigaChat-провайдер LiteLLM (`gigachat/GigaChat-2-Max`), у которого есть свой OAuth-цикл и собственная логика преобразования.
2. **Нативное преобразование LiteLLM** (поставляется с библиотекой, мы только пользуемся):
   - поле `tools` переименовывается в `functions`;
   - значение `tool_choice="required"` понижается до `"auto"` (GigaChat не поддерживает `required`);
   - содержимое сообщений с ролью `tool` заворачивается в JSON, если оно ещё не строка;
   - если `temperature == 0`, выставляется `top_p == 0`.
3. **Колбэк `provider_normalizer`** применяет к схеме каждого инструмента pipeline из 9 walker'ов (5 наших + 4 ported из `ai-forever/gpt2giga`):
   - `_strip_strict_flag` — удаляет `"strict": true` с function spec'а (нестандартный для GigaChat OpenAPI keyword);
   - `_ensure_object_properties` — добавляет `properties: {}` к каждому `type: object` без него;
   - `_ensure_field_types` — добавляет `type: object` (с пустым `properties: {}`) к полям без `type`;
   - `_normalize_additional_properties` — `additionalProperties: <sub-schema>` → `false`;
   - `_strip_unsupported_keywords` — удаляет рекурсивно `default`, `minimum`, `maximum`, `pattern`, `format`, `minLength`, `maxLength`, `multipleOf` и т.п.;
   - **`_resolve_refs`** (ported) — inline `$ref` / `$defs` references (pydantic-emitted схемы часто содержат);
   - **`_flatten_anyof_oneof`** (ported) — `anyOf`/`oneOf` → первый non-null variant, merge sibling metadata;
   - **`_normalize_nullable_types`** (ported) — `type: ["string", "null"]` → `type: "string"`;
   - **`_remap_reserved_tool_names`** (ported) — `web_search`, `text2image`, `text2model3d`, `get_file_content` → `__user_<name>` (избегает коллизий с built-in GigaChat functions).
4. `drop_params: true` убирает из запроса поля, которые GigaChat не принимает (например, `top_p` если осталось от пользовательских настроек, OpenRouter-заголовки и т.п.).

Агент про эти преобразования не знает — он видит только код ответа 200 и стандартный JSON в формате OpenAI.

**Важно**: часть требований снимается на уровне source (нативные tool-схемы в репо уже совместимы с OpenAI strict mode под флагом `openai_strict_tools_schema: true`), но GigaChat-специфика этим не покрывается полностью: нужны `properties: {}` на пустых object'ах, `type` на полях которые могут появиться от MCP-серверов (где нет контроля над схемой), и стрип `default`/`minimum` в любых tool-схемах. Поэтому GigaChat обслуживается через прокси, не через прямой вызов.

## Преобразования тела запроса под Yandex Cloud

У Yandex нет нативного провайдера в LiteLLM, поэтому задействуется generic-маршрут `openai/...` с явным `api_base`:

```yaml
- model_name: yandex-pro
  litellm_params:
    model: openai/gpt://${YANDEX_FOLDER_ID}/yandexgpt/latest
    api_base: https://llm.api.cloud.yandex.net/v1
    api_key: "os.environ/YANDEX_API_KEY"
```

Колбэк `provider_normalizer` решает **две** задачи:

1. **Pre-call**: подставляет пробел вместо пустого `content` в каждом сообщении. Без этого Yandex отвечает HTTP 400 на сообщения, в которых ассистент вернул только `tool_calls` без текста.
2. **Post-call**: парсит `content` ответа, если он содержит JSON-объект (часто обёрнутый в ` ```...``` ` markdown-fence) и собирает синтетический `tool_calls` массив. Generic-алгоритм матчит ключи payload'а с `tools[*].function.parameters.properties.keys()` из исходного запроса — никаких hardcoded имён tools. Очищает `content`, ставит `finish_reason: "tool_calls"`. См. `_synthesize_tool_calls_for_yandex` в реализации.

**Эмпирически**: после добавления post-call hook'а Yandex multi-turn tool round-trip начинает работать на простых сценариях (single-tool circles). Раньше зависал из-за того что LiteLLM `openai/...` маршрут не имеет нативного yandex-провайдера, и Yandex модель отвечает proprietary-форматом (JSON-в-content) который LiteLLM не парсит обратно в OAI `tool_calls`. Post-call hook закрывает этот gap на нашей стороне.

Длинные многошаговые цепочки tool-вызовов на Yandex наблюдались устойчивыми не во всех случаях — модель иногда возвращает gibberish с лишними полями. Для агентного режима основным провайдером лучше брать GigaChat / OpenRouter, Yandex использовать как secondary.

## Преобразования тела запроса под прочих провайдеров

Из коробки сейчас поставляются только GigaChat и YandexGPT. Любой другой OpenAI-совместимый эндпоинт (OpenRouter, Ollama, vLLM, llama.cpp, кастомный inference) подключается одной записью в `model_list` через generic-маршрут `openai/...` либо нативный LiteLLM-маршрут (`openrouter/...`, `ollama_chat/...`) — см. раздел «Подключение своего провайдера».

## Структура агентского `config.yml`

Поля backend, релевантные для подключения провайдеров:

| Поле | Тип | Назначение |
|---|---|---|
| `backend.api_mode` | `"chat_completions"` \| `"responses"` | Режим API (для прокси-пути — всегда `chat_completions`) |
| `backend.completions_url` | string | URL endpoint'а. Для прокси-пути — `http://localhost:4000/v1/chat/completions` |
| `backend.chat_completions_max_tokens` | int | Лимит токенов на ответ |
| `backend.chat_completion_kwargs` | dict | Произвольные OpenAI-совместимые параметры (temperature, top_p, etc.). С защитой системных полей — см. раздел «Защита системных полей» |
| `backend.chat_completion_headers` | dict | HTTP-заголовки запроса. Сюда кладётся `Authorization: "Bearer <key>"` для прокси или провайдера |
| `backend.use_max_tokens_param` | bool | `false` (default) → шлём `max_completion_tokens`. `true` → шлём `max_tokens` (legacy OpenAI API) |
| `backend.openai_strict_tools_schema` | bool | `true` → форсит для всех tools `required = list(properties.keys())` + `additionalProperties: false`. Требуется для прямого OpenAI strict mode |
| `backend.use_ssl` | bool | SSL verify |
| `backend.use_retry` | bool | retry policy |

Эти поля уже есть в `src/config_modules/backend.py::BackendConfig` и пробрасываются в плоский `config_dict` через `src/config.py::load_config`.

## Текущее состояние кода (не требует доработки)

В `src/` уже сделано:

| Файл | Что |
|---|---|
| `src/utils/connect_backend.py::create_call_data_completions` | Минимальный OAI payload (`model`, `messages`, `tools`, `tool_choice`, `max_tokens`/`max_completion_tokens`) + мердж `chat_completion_kwargs` с защитой системных полей |
| `src/utils/connect_backend.py::create_call_headers` | Универсальный мердж `chat_completion_headers` поверх системных заголовков |
| `src/utils/llm_chat_client.py::request_chat_completion_text` | Тот же payload-pipeline для summary-flow и subagent'ов |
| `src/tools/__init__.py::_apply_openai_strict_tools_schema` | Условная нормализация tool schemas под OpenAI strict под флагом |
| `src/tools/simple_tools/execute_command.py`, `run_python_code.py` | Поле `env` хранится как `env_vars: array<{name,value}>` (совместимо с OpenAI strict + парсится обратно в `dict[str,str]`) |
| `src/tools/simple_tools/submit_solution.py` | Поле `handoff: type:string` (stringified JSON, парсится декоратором `@parse_json_object_argument`) |
| `src/tools/skills/skills_tools.py::skills_run_script` | Поле `args: type:string` (stringified JSON) |
| `src/tools/simple_tool_base.py` | Хуки `tool_output_on_cycle_stop()` + `fallback_tool_output_on_cycle_stop()` |
| `src/utils/arg_decorators.py` + `src/utils/json_arg_parser.py` | Декоратор `@parse_json_object_argument(source_arg, target_arg)` для парсинга stringified-JSON полей |
| `src/message_policy/chat_completions.py::repair_tool_call_pairing` | Удаляет orphan tool_calls из истории при старте run в `chat_completions` mode |
| `src/message_policy/cycle_stop.py::append_cycle_stop_tool_outputs_for_batch` | Синтезирует `function_call_output` для не-исполненных tool_calls (например при early `submit_solution`) |
| `src/agent_executor.py` | Вызов `repair_tool_call_pairing` на старте run |
| `src/processing_responses.py` | Wrap `validate_final_report_vs_user_intent` в try/except (fail-open); интеграция с `cycle_stop` |
| `demo_config_openai.yml` | Demo config для прямого вызова `api.openai.com` с `openai_strict_tools_schema: true` + Bearer auth |

Этот код покрывает **прямой путь на OpenAI** или strict-совместимый эндпоинт. Для подключения провайдеров через прокси в `src/` дополнительных правок почти не требуется (см. раздел «Что осталось доработать»).

## Реализованные блоки multi-provider

1. ✅ **Подпапка `litellm/` создана** по раскладке выше: `proxy_config.yaml`, `callbacks/` (split на `_common`/`_gigachat`/`_yandex`/dispatcher), `install.py`, `run.py`, `README.md`, `.env.example`, `.gitignore`.

2. ✅ **`provider_normalizer.py` реализован** как тонкий dispatcher; вся vendor-логика в модулях `callbacks/_gigachat.py` и `callbacks/_yandex.py`:
   - GigaChat pre-call — 9 walker'ов (`_strip_strict_flag`, `_ensure_object_properties`, `_ensure_field_types`, `_normalize_additional_properties`, `_strip_unsupported_keywords`, `_resolve_refs`, `_flatten_anyof_oneof`, `_normalize_nullable_types`, `_remap_reserved_tool_names`).
   - GigaChat post-call — 3-level synth (L1 fenced JSON / L2 `<|superquote|>` / L3 wrap в `submit_solution`).
   - Yandex pre-call — `_backfill_empty_content`.
   - Yandex post-call — generic `synthesize_tool_calls` (матчит payload-keys с `tools[*].function.parameters.properties.keys()`, без hardcoded имён).

3. ✅ **`install.py` + `run.py` готовы**, зеркалят паттерн `recommended_mcp/`:
   - `install.py` — `pip install 'litellm[proxy]==1.83.14'` через subprocess (idempotent при совпадении версии) + seed `.env`. Чистый stdlib, без Docker.
   - `run.py` — читает `.env`, рендерит `proxy_config.local.yaml` с `__VAR__` подстановкой, sanity-checks `callbacks.provider_normalizer` через subprocess, запускает `litellm` CLI с `cwd=litellm/`.
   - **НЕ зависят от `src/`** и не контролируют lifecycle из агента.

## Что осталось доработать

### Средний приоритет — фиксы для текущего поведения

4. **`processing_responses.check_mode` — добавить `'final_text'` mode** в `src/processing_responses.py:194-209`. Проявляется когда модель возвращает финальный text без вызова `submit_solution` (наблюдалось на GigaChat Lite/Pro/Max и Yandex). Не зависит от вендора. По факту generic `except Exception` в `run_agent` уже ловит `ValueError('Undefined mode')` и пушит technical feedback — поведение корректное, но noise в логах со stack trace на каждом цикле. Patch — косметика, токены не экономит:
   ```python
   # перед raise ValueError('Undefined mode'):
   if messages[-1].get('role') == 'assistant' and messages[-1].get('content'):
       return 'final_text'
   ```
   В `run_agent` обработать `mode == 'final_text'` как `tool_error_cnt+=1` + technical feedback (тот же flow, без stack trace).

   *Статус:* пробовали добавить (B2), но откатили — для Yandex не нужно (5/5 clean без правок), для GigaChat не экономит токены. Чисто косметика логов.

5. **`stop_tools` фильтр глобально** в `src/tools/__init__.py::build_tools_data`. Текущая реализация фильтрует только `simple_tools` (через `get_simple_tools_data`); skills, MCP, subagents просачиваются. Bug найден при тесте: указан `skills_load` в `stop_tools`, но тулы skills продолжали дёргаться. Patch:
   ```python
   # после построения всех tools_data, перед нормализацией spec'ов:
   stop_tools_cfg = config_dict.get("stop_tools", [])
   if isinstance(stop_tools_cfg, list):
       stop_tools = {t.strip() for t in stop_tools_cfg if isinstance(t, str) and t.strip()}
       if stop_tools:
           tools_data = {n: d for n, d in tools_data.items() if n not in stop_tools}
   ```

   **Дополнительный практический мотив** (выявлен при прогонах GigaChat Max): tools spec на 20+ инструментах = ~3-8k tokens в каждом backend call. Без глобального `stop_tools` нельзя реально снизить расход токенов на провайдерах с дорогим биллингом (Sber Freemium spent ~27k tokens на одну простую задачу). Это **upgrade приоритета** до высокого.

### Среднее-низкое — pre-backend orphan repair

6. **Mid-run `repair_tool_call_pairing`** в `src/processing_responses.py::_request_backend_canonical` для chat_completions mode. Гипотетический фикс: если `apply_tool` падает с `WrongFunctionArgumentsException`, agent кладёт `technical user message` в state.messages БЕЗ соответствующего `function_call_output` — orphan tool_call. Strict-валидаторы (GigaChat 422, Yandex 400) отвергают такую историю. Patch:
   ```python
   sanitized_state_messages = repair_tool_call_pairing(
       sanitized_state_messages, keep_latest_unanswered=False,
   )
   ```

   *Статус:* пробовали (B1), откатили — для Yandex не воспроизводится в наших прогонах (post-call synth `tool_calls` закрывает gap). Для GigaChat не успели верифицировать на новом аккаунте.

### Низкий приоритет — UX-чистка

7. **Зафиксировать в `litellm/install.py`** версию `litellm[proxy]==1.83.14` (актуальная стабильная после security incident в марте 2026; рекомендуется ≥1.83). В `pyproject.toml` НЕ объявляем — `install.py` сам делает `pip install` (см. раздел «Раскладка `litellm/`»).

8. **Документация в `litellm/README.md`** — quick-start, таблица alias → upstream → required env vars, секция «Adding exotic provider».

9. **Smoke-тесты на CI** — unit-тесты колбэков на каждом PR; smoke-тесты на 4 провайдерах с реальными credentials по labels или nightly cron.

### Вне правок

| Файл | Почему не трогаем |
|---|---|
| `src/run_context.py` | Provider-OOP-классы внутри агента не вводятся — все знание о провайдерах вынесено в прокси или уже покрыто флагом `openai_strict_tools_schema` |
| `src/cli.py` | CLI-флаги работают как раньше; пользовательские поля можно класть через `--chat_completion_kwargs '{"temperature": 0.5}'` |

## Защита системных полей

Поле `chat_completion_kwargs` в `config.yml` позволяет пользователю донастроить любой OpenAI-совместимый параметр. Чтобы пользовательская настройка не сломала работу агента, при мердже системные поля защищены:

```python
SYSTEM_FIELDS = frozenset({
    "model", "messages", "tools", "tool_choice",
    "max_tokens", "max_completion_tokens",
})
```

Если пользователь положит в `chat_completion_kwargs` что-то из этого списка — поле игнорируется, в лог пишется предупреждение. Системные значения берутся из состояния агента (модель из `config.agent.model`, сообщения из истории, и т.д.).

Флаг `backend.use_max_tokens_param: bool` управляет именем поля токенов. По умолчанию `false` → используется `max_completion_tokens` (актуально для reasoning-моделей вроде `o1` / `o3-mini`, которые требуют это поле). При `true` → используется устаревший `max_tokens` (для совместимости со старыми OpenAI API).

## Подключение своего провайдера

Два сценария — простой (большинство OpenAI-совместимых апстримов) и сложный (требуется структурное преобразование тела).

### Простой сценарий — одна запись в `model_list`

Подходит, когда апстрим принимает запросы в формате OpenAI Chat Completions, отвечает им же, и единственное, что нужно — указать адрес и ключ. Дополнительные поля (если требуются) кладутся в `extra_body`.

1. Дописать запись в `model_list` файла `litellm/proxy_config.yaml`:
   ```yaml
   model_list:
     - model_name: my-internal-llama
       litellm_params:
         model: openai/llama3-70b-finetuned
         api_base: http://my.internal.host:9000/v1
         api_key: os.environ/MY_API_KEY
         extra_body:
           # опционально — поля, которых требует ваш апстрим
           custom_temperature_strategy: aggressive
   ```

   Любое значение вида `__VAR__` будет подставлено `run.py`-ем из `.env` — удобно для PII (folder_id, region и т.п.).

2. Прописать credentials в `litellm/.env`.

3. Перезапустить прокси:
   ```bash
   python litellm/run.py
   ```

4. Использовать в агенте:
   ```bash
   gc-cli -q "..." --model my-internal-llama
   ```

### Сложный сценарий — `model_list` + собственный модуль в `callbacks/`

Нужен, если апстрим требует **структурных** преобразований тела запроса или ответа: рекурсивного обхода JSON Schema, подстановки пустых полей, переноса данных между полями ответа и т.п. Это нельзя выразить набором YAML-полей — алгоритм пишется на Python.

К четырём шагам выше добавляются:

5. Создать файл с логикой в `litellm/callbacks/_<vendor>.py` (зеркало `_gigachat.py` / `_yandex.py`):

   ```python
   # litellm/callbacks/_my_vendor.py
   from typing import Any

   from callbacks._common import (
       get_message_field, get_message_from_choice, iter_response_choices,
       set_choice_finish_reason, set_message_tool_call,
   )

   def pre_call(data: dict) -> None:
       # mutate data["messages"], data["tools"], ...
       ...

   def post_call(response: Any, tools: list[dict]) -> None:
       # mutate response in-place
       ...
   ```

6. Подключить в dispatcher `litellm/callbacks/provider_normalizer.py`:

   ```python
   from callbacks import _gigachat, _yandex, _my_vendor

   # в async_pre_call_hook:
   if model.startswith("my-vendor"):
       _my_vendor.pre_call(data)
   ```

7. Перезапустить прокси.

В обоих сценариях правки изолированы в подпапке `litellm/`. Код агента (`src/`) не трогается.

## Запуск прокси (install.py + run.py)

Прокси — **отдельный процесс**, агент про него **ничего не знает кроме URL** в `backend.completions_url`. Это сознательное архитектурное решение (см. `principles.md` — низкое зацепление между процессами; каждый процесс живёт независимо).

Никакого auto-start из агента, никакого `proxy_manager` в `src/`, никаких CLI-подкоманд вида `gc-cli proxy status/stop/restart`. CLI остаётся CLI, прокси остаётся прокси.

Два скрипта в `litellm/` зеркалят паттерн `recommended_mcp/install.py` + `recommended_mcp/run.py`:

| Скрипт | Что делает |
|---|---|
| `litellm/install.py` | Проверяет что `litellm` (PyPI-пакет) импортируется; если `litellm/.env` отсутствует — копирует из `.env.example`. Чистый stdlib, кросс-платформенный. |
| `litellm/run.py` | Читает `litellm/.env` → проверяет импорт `callbacks.provider_normalizer` → `subprocess` запускает `python -m litellm --config proxy_config.yaml --port 4000`. `cwd = litellm/` (чтобы импорт `callbacks` работал через `sys.path[0]`). Foreground, останавливается по Ctrl-C. |

Полный цикл от чистого клона до работающего агента — четыре команды:

```bash
python litellm/install.py                                # 1. dep check + .env seed
$EDITOR litellm/.env                                     # 2. ввод credentials
python litellm/run.py                                    # 3. запуск прокси (foreground)
gc-cli -q "..." --config config_gigachat_proxy.yml       # 4. использование (отдельный терминал)
```

### Что осознанно вне scope

- Auto-start прокси из `gc-cli`
- `src/utils/proxy_manager.py`
- CLI-подкоманды `gc-cli proxy status/stop/restart`
- `--no-auto-proxy` флаг
- Docker / docker-compose
- Health-check polling из агента
- PID-файлы и file locks
- Server/swarm-mode оркестрация прокси

«Пока комбайн не надо собирать» — прокси-процесс запускается руками (локально) либо системным supervisor'ом (DevOps). Орекстрация добавится отдельной задачей, когда появятся другие компоненты для оркестрации.

## Совместимость и режимы запуска

| Провайдер | Возможен ли запуск без прокси | Почему | Эмпирическая проверка |
|---|---|---|---|
| OpenAI | да | чистый OpenAI Chat Completions, `openai_strict_tools_schema: true` покрывает | ✅ через `demo_config_openai.yml` |
| OpenRouter | да | работает по чистому OpenAI Chat Completions, заголовки через `chat_completion_headers` | ✅ smoke через прокси |
| Ollama (без thinking-mode) | да | работает по OpenAI-совместимому API | ✅ smoke через прокси |
| Любой OpenAI-совместимый эндпоинт (vLLM, llama.cpp, кастомный inference) | да | провайдерные поля выражаются через `chat_completion_kwargs` | ✅ архитектура подтверждена |
| **GigaChat** | **нет** | требуется рекурсивная правка JSON Schema инструментов (через колбэк), нельзя выразить набором YAML-полей | ✅ архитектура clean на Lite/Pro/Max; **только Max** доводит до `submit_solution` (Lite/Pro не вызывают финализатор) |
| **Yandex Cloud** | **нет** ⚠️ | требуется подстановка `content` (pre-call) и синтез `tool_calls` из JSON-в-content (post-call) | ✅ **5/5 clean** через прокси+callback; **0/5** прямой путь (HTTP 400 `empty message text` на каждом multi-turn) |
| Ollama qwen3 thinking-mode | нет | требуется перенос `reasoning_content` → `content` в ответе | (не тестировался в текущем PoC) |

Прямой режим (без прокси) — это совместимость с текущим способом работы. Поднятие прокси — рекомендуемый путь для всех новых конфигураций.

## Что в код проекта НЕ попадает

После реализации в `src/` не должно быть:

- упоминаний имён провайдеров (`gigachat`, `yandex`, `sber`, `openrouter`, `ollama`);
- классов вида `GigaChatProvider`, `YandexProvider`;
- блоков `if model.startswith(...)`;
- захардкоженных провайдерных полей (`top_k`, `repetition_penalty`, `chat_template_kwargs`, `separate_reasoning`).

Проверка: `grep -rE 'gigachat|yandex|sber' src/` должен возвращать пустой результат.

Все знание о провайдерах — в `litellm/`:
- `litellm/proxy_config.yaml` (канонический набор моделей + регистрация callback);
- `litellm/callbacks/provider_normalizer.py` (dispatcher) + `litellm/callbacks/_<vendor>.py` (логика конкретного провайдера).

Эти файлы можно обновлять независимо от логики агента и от схемы конфигов в `src/`.

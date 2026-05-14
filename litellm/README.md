# Батарейка LiteLLM Proxy

Самодостаточная настройка LiteLLM Proxy для маршрутизации gc-cli (или любого клиента, говорящего по OpenAI Chat Completions) на несколько LLM-провайдеров. Из коробки заведены GigaChat-2 и YandexGPT; добавление любого OAI-совместимого апстрима — одна запись в `proxy_config.yaml`.

Эта директория — **независимая батарейка**. Она НЕ зависит от `src/`, и `src/` НЕ зависит от неё. Единственный контракт между агентом и прокси — HTTP-URL.

Сам `litellm` **не объявлен в `pyproject.toml`** — `install.py` единственная точка, которая его ставит. Это зеркалит паттерн `recommended_mcp/install.py` и не тянет `litellm[proxy]` (~200 МБ транзитивных зависимостей: uvicorn/fastapi/orjson/…) тем, кто прокси не использует.

## Лицензия

LiteLLM © 2023 Berri AI — [MIT](https://github.com/BerriAI/litellm/blob/main/LICENSE). Версия пиннется в `install.py`.

## Раскладка

```
litellm/
├── proxy_config.example.yaml  # шаблон под git: алиасы моделей + dummy credentials
├── proxy_config.yaml          # ЛОКАЛЬНЫЙ конфиг (gitignored), реальные api_key / folder_id
├── agent_config_examples/     # ПРИМЕРЫ agent-конфигов под прокси (под git, read-only)
│   ├── config_gigachat.yml
│   └── config_yandex.yml      # копируйте/правьте под себя вручную
├── callbacks/
│   ├── __init__.py
│   ├── provider_normalizer.py # тонкий dispatcher: класс + proxy_handler_instance
│   ├── _common.py             # vendor-agnostic helpers (JSON-парсинг, tool_call shape, …)
│   ├── _gigachat.py           # 9 walker'ов pre-call + 3-level synth post-call
│   └── _yandex.py             # backfill пустого content + JSON-in-content → tool_calls
├── install.py                 # кросс-платформенный pip-install + seed proxy_config.yaml
├── run.py                     # кросс-платформенный лончер (sanity-check + subprocess litellm)
└── .gitignore
```

## Быстрый старт

```bash
# 1. Один раз — pip-install litellm[proxy] + seed proxy_config.yaml из proxy_config.example.yaml
python litellm/install.py

# 2. Заполнить api_key и folder_id для каждого провайдера
$EDITOR litellm/proxy_config.yaml

# 3. Запуск прокси (foreground, держите терминал открытым)
python litellm/run.py

# 4. (Опционально) Скопировать пример agent-конфига в корень репы или использовать напрямую
cp litellm/agent_config_examples/config_gigachat.yml config.yml      # дефолт gc-cli
# либо без копирования:
gc-cli -q "..." --config litellm/agent_config_examples/config_gigachat.yml --model gigachat-2-lite
```

## Конфигурация

### `proxy_config.yaml`

Список алиасов моделей. Каждый алиас маппится в пару провайдер+модель:

- `gigachat-2-lite`, `gigachat-2-pro`, `gigachat-2-max` → нативный провайдер LiteLLM `gigachat/`
- `yandex-gpt-lite`, `yandex-gpt-pro` → generic-маршрут `openai/` на Yandex Cloud OAI-compat endpoint

Сам `proxy_config.yaml` **gitignored** — содержит реальные `api_key`, `folder_id` и прочие credentials в открытом виде. Под git лежит только шаблон `proxy_config.example.yaml` с dummy-значениями типа `<BASE64(...)>`. При первом запуске `install.py` копирует `.example.yaml` → `.yaml`, после чего юзер вписывает реальные значения руками.

### `callbacks/`

Логика вендорных особенностей разложена по модулям:

- **`_common.py`** — vendor-agnostic helpers (парсинг JSON, сборка tool_call envelope, доступ к pydantic/dict полям).
- **`_gigachat.py`** — pre-call pipeline из 9 walker'ов (фиксы под strict JSON Schema GigaChat) + post-call 3-level synth (вытаскивание plain-text ответов в нативные `tool_calls`).
- **`_yandex.py`** — pre-call `backfill_empty_content` (Yandex отвергает пустой assistant `content`) + post-call `synthesize_tool_calls` (парсинг JSON-в-контенте → нативные `tool_calls`).
- **`provider_normalizer.py`** — тонкий dispatcher. Маршрутизация по `data["model"].startswith(...)`, без центрального реестра. Добавление нового провайдера = новый `_<vendor>.py` + `if`-блок в dispatcher.

## Добавление своего провайдера

См. раздел «Подключение своего провайдера» в `gc-providers-spec.md`.

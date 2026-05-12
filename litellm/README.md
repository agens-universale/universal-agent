# Батарейка LiteLLM Proxy

Самодостаточная настройка LiteLLM Proxy для маршрутизации gc-cli (или любого клиента, говорящего по OpenAI Chat Completions) на несколько LLM-провайдеров: GigaChat, YandexGPT, OpenRouter, Ollama, кастомные OAI-совместимые эндпоинты.

Эта директория — **независимая батарейка**. Она НЕ зависит от `src/`, и `src/` НЕ зависит от неё. Единственный контракт между агентом и прокси — HTTP-URL.

Сам `litellm` **не объявлен в `pyproject.toml`** — `install.py` единственная точка, которая его ставит. Это зеркалит паттерн `recommended_mcp/install.py` и не тянет `litellm[proxy]` (~200 МБ транзитивных зависимостей: uvicorn/fastapi/orjson/…) тем, кто прокси не использует.

## Раскладка

```
litellm/
├── proxy_config.yaml          # алиасы моделей + регистрация колбэка
├── callbacks/
│   ├── __init__.py
│   └── provider_normalizer.py # pre/post-call хуки (схемы GigaChat + контент Yandex)
├── install.py                 # кросс-платформенный pip-install + seed .env
├── run.py                     # кросс-платформенный лончер
├── .env.example               # шаблон credentials
├── .env                       # реальные credentials (gitignored)
└── .gitignore
```

## Быстрый старт

```bash
# 1. Один раз — pip-install litellm[proxy] и seed .env
python litellm/install.py

# 2. Заполнить credentials
$EDITOR litellm/.env

# 3. Запуск прокси (foreground, держите терминал открытым)
python litellm/run.py

# 4. Использование из gc-cli (в отдельном терминале)
gc-cli -q "..." --config config_gigachat_proxy.yml --model gigachat-2
```

## Конфигурация

### `proxy_config.yaml`

Список алиасов моделей. Каждый алиас маппится в пару провайдер+модель:

- `gigachat`, `gigachat-2`, `gigachat-pro`, `gigachat-max` → нативный провайдер LiteLLM `gigachat/`
- `yandex-gpt-lite`, `yandex-gpt` → generic-маршрут `openai/` на Yandex Cloud
- `openrouter-qwen-coder`, `openrouter-claude` → нативный провайдер LiteLLM `openrouter/`
- `ollama-qwen` → нативный провайдер LiteLLM `ollama_chat/`

### `callbacks/provider_normalizer.py`

Один файл со всеми vendor-quirks:

- **GigaChat pre-call:** нормализация tool-схем девятью walker'ами (фиксы под strict JSON Schema)
- **GigaChat post-call:** трёхуровневый `_synthesize_tool_call_for_gigachat` (вытаскивание plain-text ответов в нативные `tool_calls`)
- **Yandex pre-call:** `_backfill_empty_content` (Yandex отвергает пустой assistant content)
- **Yandex post-call:** `_synthesize_tool_calls_for_yandex` (парсинг JSON-в-контенте в нативные `tool_calls`)

Роутинг по `data["model"].startswith(...)` — без центрального реестра провайдеров. Добавить нового провайдера = новая запись в `model_list` + (опционально) `if`-блок в колбэке.

## Добавление своего провайдера

См. раздел «Подключение своего провайдера» в `gc-providers-spec.md`.

## Лицензия

Сам LiteLLM — MIT-лицензия, см. [github.com/BerriAI/litellm/blob/main/LICENSE](https://github.com/BerriAI/litellm/blob/main/LICENSE). Версия пиннется в `litellm/install.py` (константа `LITELLM_PIN`). Код в этой директории — часть проекта gc-cli-pub и следует его репозиторной лицензии.

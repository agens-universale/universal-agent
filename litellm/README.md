# LiteLLM Proxy Battery

Self-contained LiteLLM Proxy setup for routing gc-cli (or any OpenAI-Chat-Completions-shaped client) to multiple LLM providers: GigaChat, YandexGPT, OpenRouter, Ollama, custom OAI-compat endpoints.

This directory is an **independent battery**. It does NOT depend on `src/` and `src/` does NOT depend on it. The only contract between agent and proxy is the HTTP URL.

## Layout

```
litellm/
├── proxy_config.yaml          # model aliases + callback registration
├── callbacks/
│   ├── __init__.py
│   └── provider_normalizer.py # pre/post-call hooks (GigaChat schema + Yandex content fixes)
├── install.py                 # cross-platform dep check + .env seed
├── run.py                     # cross-platform launcher
├── .env.example               # credentials template
├── .env                       # real credentials (gitignored)
└── .gitignore
```

## Quick start

```bash
# 1. Once — verify deps and seed .env
python litellm/install.py

# 2. Edit credentials
$EDITOR litellm/.env

# 3. Run proxy (foreground, keep terminal open)
python litellm/run.py

# 4. Use from gc-cli (separate terminal)
gc-cli -q "..." --config config_gigachat_proxy.yml --model gigachat-2
```

## Configuration

### `proxy_config.yaml`

Lists model aliases. Each alias maps to a provider+model:

- `gigachat`, `gigachat-2`, `gigachat-pro`, `gigachat-max` → native LiteLLM `gigachat/` provider
- `yandex-gpt-lite`, `yandex-gpt` → generic `openai/` route to Yandex Cloud
- `openrouter-qwen-coder`, `openrouter-claude` → native LiteLLM `openrouter/` provider
- `ollama-qwen` → native LiteLLM `ollama_chat/` provider

### `callbacks/provider_normalizer.py`

Single file containing all vendor quirks:

- **GigaChat pre-call:** 9-walker normalization of tool schemas (strict JSON Schema fixes)
- **GigaChat post-call:** 3-level `_synthesize_tool_call_for_gigachat` (rescue plain-text replies into native tool_calls)
- **Yandex pre-call:** `_backfill_empty_content` (Yandex rejects empty assistant content)
- **Yandex post-call:** `_synthesize_tool_calls_for_yandex` (parse JSON-in-content into native tool_calls)

Routing is by `data["model"].startswith(...)` — no central provider registry. Adding a new provider = add a `model_list` entry + (optional) an `if` block in the callback.

## Adding a new provider

See `gc-providers-spec.md` section «Подключение своего провайдера».

## License

LiteLLM itself is MIT — see [github.com/BerriAI/litellm/blob/main/LICENSE](https://github.com/BerriAI/litellm/blob/main/LICENSE). We pin `litellm[proxy]==1.83.14` in the project's `pyproject.toml`. The code in this directory is part of the gc-cli-pub project and follows its repo license.

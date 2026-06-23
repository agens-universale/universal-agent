# Корп-зеркало агента: тонкий config-оверлей (вариант A)

Дата: 2026-06-23
Статус: заметка-инструкция (не закоммичена намеренно).

## Решение

Зеркалим агента в корп source control и раздаём установку **из корп-репо** (`uv tool install git+<корп-репо>`). Поскольку при git-установке к юзеру попадает ровно `config.default.yml` из репо, корп-различия живут **в самом корп-репо** как тонкий оверлей.

- **Берём вариант A** (корп-дефолты в `config.default.yml` корп-ветки). Не вариант B (generic + внешний провижининг), потому что при git-раздаче отдельного шага доставки конфига нет — B потребовал бы вручную/через MDM доставлять конфиг каждому, это больше работы, не меньше.
- Код **общий, не форкаем**. Отличается только **конфиг + URL-ы** (данные, не логика). Отдельную функциональную ветку заводить НЕ нужно, пока не появится реальный корп-только код.
- B станет правильным позже — **если** появится корп-CI, публикующий wheel в корп-индекс (тогда корп-конфиг инжектится на сборке, исходник остаётся generic).

## Что отличает корп-поставку (полный список)

Всего **3 области**, всё — данные/URL:
1. `config.default.yml` — модель и эндпоинт.
2. `recommended_mcp/config.yml` — `repo_url` (откуда тянуть source-MCP).
3. Install-команда в доках — URL корп-репо.

## Точные правки (from → to)

### 1. `config.default.yml` — блок `backend` + `agent.model`

Сейчас (внешний дефолт, SGLang):
```yaml
backend:
  completions_url: "http://your-backend:PORT/v1/chat/completions"
  use_ssl: false
  use_max_tokens_param: true
  chat_completion_headers:
    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"
agent:
  model: "gigacode_agent_v1_rc"
```

Корп (vLLM/DeepSeek в контуре — модель/путь из памяти проекта; хост, токен/auth-endpoint берёшь из провижининга):
```yaml
backend:
  completions_url: "https://<КОРП-ХОСТ>/api/v2/chat/completions"   # ВНИМАНИЕ: /api/v2 (не v1)
  use_ssl: true                                                    # https
  use_max_tokens_param: true                                       # vLLM ждёт стандартный max_tokens (проверь)
agent:
  model: "vllm/DeepSeek-V4-Flash-262k"                             # или -131k (отличие — окно контекста)
```
Грабли: `CodeAgent` — НЕ chat-модель, на `chat/completions` отдаёт 404. Брать именно `DeepSeek-V4-Flash-*`.

**Авторизация к модели — два варианта (выбрать один):**

Вариант 1 — статический токен (если контур выдаёт токен строкой):
```yaml
backend:
  chat_completion_headers:
    Authorization: "Bearer <ТОКЕН>"
```

Вариант 2 (рекомендуется для контура) — `agent_auth`: агент сам выпускает JWT, токен в конфиге не хранится:
```yaml
agent_auth:
  auth_endpoint: "https://<КОРП-AUTH-ENDPOINT>"   # ОБЯЗАТЕЛЬНОЕ
  login: "<логин>"                 # опц. (дефолт "login")
  env_name: "surface_cli"          # опц. (дефолты)
  env_version: "0.0.1"
  plugin_name: "surf-cli-plugin"
  plugin_version: "0.0.1"
  # device_code / device_code_hex — опц.
```
При `agent_auth` поле `backend.chat_completion_headers.Authorization` задавать НЕ нужно — `AuthConnector` на каждый запрос получает свежий JWT с `auth_endpoint` и сам ставит `Authorization: Bearer <jwt>` (`verify` = `use_ssl`). Это и есть «подключение без хранения токена». Прод-модель в контуре идёт именно через **agent_auth JWT** → для контура это предпочтительный вариант.

### 2. `recommended_mcp/config.yml` — `repo_url`

Сейчас:
```yaml
repo_url: https://gitverse.ru/gigacode-ai/background-agent
```
Корп (чтобы source-MCP `office/science/computer` в method-2 тянулись из корп-git, а не из внешнего gitverse):
```yaml
repo_url: https://<КОРП-SOURCE-CONTROL>/<group>/<repo>
```

### 3. Install-команда в доках

В `docs/gc-ua-install.html` (шаг 2) и `docs/install-quickstart-ru.md` (шаг 2) — заменить URL внешнего gitverse на корп-репо. Если раздаёшь из дефолтной ветки (`main`/`master`) — убрать `@feat/...`:
```bash
uv tool install "git+https://<КОРП-SOURCE-CONTROL>/<group>/<repo>" --system-certs
```
Там же:
- ссылка в шапке `gc-ua-install.html` «Репозиторий в GitVerse» → URL корп-репо (и текст под площадку);
- троблшутинг про корп TLS (`--system-certs`) и PATH — оставить как есть, актуально и в контуре.

### (опционально) Корп-MCP по mTLS

Если в контуре подключаются корп-MCP (sbertrack/jira/confluence), можно пред-засеять их в `config.default.yml` секцией `mcp-servers` через `stream_http` + `verify-ssl`/`client-cert`/`client-key` (поля схемы уже поддержаны). Креды/сертификаты — из провижининга.

## Процесс (как делать и синхронизировать)

```bash
# 1) зеркало
git remote add corp https://<КОРП-SOURCE-CONTROL>/<group>/<repo>
git push corp <ветка>:main          # либо нужные ветки

# 2) корп-ветка с дельтой (поверх общего кода)
git checkout -b corp main
#   ...внести правки из разделов 1–3 выше...
git commit -am "corp overlay: config.default + repo_url + install URL"
git push corp corp

# 3) раздача установки — из ветки corp корп-репо.

# 4) синхронизация при обновлении внешней версии:
git checkout corp
git merge main                       # конфликты только в 3–4 файлах оверлея, мердж почти всегда чистый
git push corp corp
```

Дельта маленькая и предсказуемая → синк дешёвый. Альтернатива merge — держать оверлей последним коммитом и `rebase corp onto main`; выбирай по вкусу команды.

## Открытые вопросы (уточнить под контур)

- **Хост и JWT** прод-эндпоинта — из корп-провижининга (в памяти их нет, только модель/путь/тип авторизации).
- **mTLS к самому backend** (не к MCP): если прод-эндпоинт за mTLS с client-cert — проверь, хватает ли текущих полей конфига (`use_ssl` + CA). Если нужен client-cert именно к backend — это может потребовать доработки кода (сейчас mTLS-поля есть у MCP-серверов, не у backend). Зафиксируй как отдельную задачу, если всплывёт.
- **Авторизация на корп-git** для установки: если корп source control требует логин — у пользователей должен быть доступ к репо (как и во внешнем gitverse). Лучше — анонимный/internal-read клон в контуре, иначе каждому нужен токен.
- **Токен модели в `config.default.yml`**: оставить плейсхолдером с подсказкой, либо подставлять провижинингом на первом запуске — зависит от того, как контур выдаёт креды.

## Чек-лист корп-поставки

- [ ] `git push` зеркала в корп source control.
- [ ] Ветка `corp` создана.
- [ ] `config.default.yml`: `completions_url` (`/api/v2`!), `use_ssl: true`, `model: vllm/DeepSeek-V4-Flash-262k`, авторизация — статический `Authorization` ЛИБО блок `agent_auth` (auto-JWT, реком.).
- [ ] `recommended_mcp/config.yml`: `repo_url` → корп-репо.
- [ ] `docs/gc-ua-install.html` + `docs/install-quickstart-ru.md`: install-URL → корп-репо, `@ветку` убрать (если из main).
- [ ] (опц.) корп-MCP mTLS пред-засеяны в `config.default.yml`.
- [ ] Проверена установка из корп-репо: `uv tool install git+<корп> --system-certs` → `gc-ua` стартует, бьёт в корп-эндпоинт.
- [ ] Описан процесс синка для команды (merge `main` → `corp`).
```

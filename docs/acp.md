[Назад к README](../readme.md)

# ACP-фасад (`gc-acp`)

Отдельный процесс `gc-acp` для IDE: редактор поднимает агента и говорит с ним по
[Agent Client Protocol](https://agentclientprotocol.com/) (JSON-RPC поверх stdin/stdout).
Тулы исполняет ядро на машине редактора; клиент показывает прозу, карточки тулов и
превью аргументов.

Это не HTTP. Для HTTP-фасадов см. [chat.md](chat.md) (`gc-chat`). Для TUI —
`gc-ua`, для разовых запросов — `gc-cli`.

## Установка

Команда входит в полный агент (`gc-ua`) и объявляется корневым мета-пакетом:

```bash
uv tool install "gc-ua @ git+https://gitverse.ru/gigacode-ai/universal-agent"
which gc-acp   # обычно ~/.local/bin/gc-acp
```

Узкий профиль (`gc-cli`, `gc-tui`, `gc-server`) команду наружу не кладёт:
`uv tool` экспортирует только скрипты запрошенного дистрибутива. Тогда отдельно:

```bash
uv tool install -e packages/acp
# без клона:
uv tool install "gc-acp @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/acp"
```

В корпоративной сети к `uv tool install` добавляйте `--system-certs`.

В клоне после `uv sync` бинарь: `<корень>/.venv/bin/gc-acp`.

## VS Code

В VS Code своего ACP-клиента нет. Поставьте расширение
[ACP Client](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client)
(`formulahendry.acp-client`):

```bash
code --install-extension formulahendry.acp-client
```

В `settings.json`:

```json
{
  "acp.agents": {
    "gc-acp": {
      "command": "/абсолютный/путь/gc-acp",
      "args": []
    }
  }
}
```

Путь — результат `which gc-acp`. Аргументы по умолчанию пустые: конфиг ищется так же,
как у `gc-cli` (без `--config`). Чтобы активировать профиль на каждый старт:

```json
"args": ["--profile", "acp"]
```

Панель ACP на боковой панели → агент `gc-acp` → Connect. В конфиге агента нужен
`os_backend: local` (это дефолт). Harbor и любой другой бэкенд процесс отклоняет
до цикла протокола: cwd сессии — путь на машине редактора.

## Запуск вручную

Редактор обычно стартует процесс сам. Для отладки:

```bash
gc-acp
gc-acp --config ./config.yml
gc-acp --config ./config.yml --profile acp
```

С первой строки **stdout принадлежит протоколу**. Диагностика и `print()` уходят в
stderr. `console_trace` процесс принудительно выключает, чтобы ядро не писало трейс
между JSON-RPC-ответами.

## Конфиг канала `acp`

Секция `acp` в общем `config.yml`. Ключи читает только `gc-acp`; `tui.*` на этот
канал не действует. Неизвестный ключ внутри `acp` загрузка отклоняет (`extra=forbid`).

| Ключ | Дефолт | Смысл |
|---|---|---|
| `acp.text_answer_mode` | `allow` | `deny` / `judge` / `allow` — проза без tool-call. В IDE обсуждение и правка доки — нормальный сценарий, поэтому дефолт `allow`, не `judge` как в TUI. |
| `acp.finish_gate_mode` | `off` | `on` / `off` / `smart` — судья на `turn_finish`. В IDE человек сам видит ответ и дифф. Значения `off` и `on` в YAML пишите в кавычках. |
| `acp.todo_reminder_steps` | `0` | Technical reminder про `session_todo` каждые N шагов. `0` — выкл. Иначе чек-лист легко толкает к «пиши код» вопреки обсуждению. |
| `acp.confirm_timeout_sec` | `600` | Сколько секунд ждать ответ IDE на `session/request_permission`. По таймауту операция отклоняется, процесс остаётся. |

После загрузки фасад пишет эти значения в runtime-ключи хода (`text_answer_mode`,
`strict_turn_finish_validation`, `finish_gate_smart`, `todo_params.reminder_steps`)
и подставляет bundled special instructions
(`chat_completions_acp_special_instructions.md`: discussion ≠ implementation).
Свой `paths.chat_completions_special_instructions_path` на этом процессе не читается.

`turn_ask_human` и `turn_intermediate_reply` дописываются в `stop_tools`: их текст
клиенту ACP не уходит (вопрос — пустой порт, промежуточный ответ — stderr). Прогресс
и вопрос пользователю модель пишет прозой ответа.

Явный профиль-оверлей — [`packages/acp/profiles/acp.yml`](../packages/acp/profiles/acp.yml).
Скопируйте в `~/.config/gc/profiles/acp.yml` (Windows: `~/.gc/profiles/`) и при
желании укажите `"args": ["--profile", "acp"]` в настройках клиента.

```yaml
acp:
  text_answer_mode: allow
  finish_gate_mode: "off"
  todo_reminder_steps: 0
  confirm_timeout_sec: 600
```

Подробнее про соседние ключи конфига — [configuration.md](configuration.md).

## Подтверждение гвардов

Вопрос задаёт гвард чтения, записи, команды или адреса с `mode=ask` и режим разрешений
(`manual`, `auto` после паузы судьи, защищённые пути): ядро останавливает тул и ждёт
человека. `gc-acp` объявляет канал «человек есть» и отправляет вопрос клиенту запросом
`session/request_permission`: четыре варианта ACP (allow once / always, reject once /
always). Пока ответа нет, тул ждёт. Отмена сессии и ошибка JSON-RPC отклоняют операцию,
ход не зависает. Если клиент не ответил за `acp.confirm_timeout_sec` (или не умеет
отвечать), модель слышит «не ответил вовремя», а не «отказал»; отказ человека в IDE —
«отказал». Запомнить выбор (always) пишет то же `permission_store`, что и баннер в TUI.
«Allow always» для команды в `edit` значит «команду и подобные» (похожую команду судья
команд пропустит сам); в `manual` и `auto` — ровно эту команду.

Новое сообщение человека снимает паузу судьи в `auto` (следующий `session/prompt`),
даже если вопрос паузы остался без ответа.

Свободный вопрос человеку сюда не входит. `turn_ask_human` в ACP выключен:
модель спрашивает прозой или финалом хода, ответ — следующий `session/prompt`.

## Режимы разрешений и одобрение плана

Режимы `edit`, `manual`, `plan` и `auto` отдаются **двумя способами**: как опция
сессии `configOptions` (`id: "mode"`, `category: "mode"`, рядом с опцией модели) и как
`modes` (список режимов и текущий) для клиентов без `configOptions`. Сменить режим можно
обоими методами — `session/set_config_option` с `configId: "mode"` и `session/set_mode`:
оба ведут в тот же путь ядра, что `Shift+Tab` в TUI и флаг `--permission-mode` в CLI. После
смены клиенту уходят `current_mode_update` и `config_option_update`. На `session/load` и
`session/resume` режим берётся из сохранённого выбора сессии, а нет его — из
`security.permissions.default_mode`. Выбор режима из IDE попадает в мету треда с
ближайшим `session/prompt` этой сессии: выбрали режим и закрыли сессию без запроса — выбор
не сохранён (как `Shift+Tab` в TUI без отправленного сообщения).

В режиме `plan` агент исследует и предлагает план инструментом `turn_propose_plan`. Его
карточка в потоке тулов скрыта: план показывается один раз **текстом ответа**, целиком, а
не урезанной карточкой (то же — при повторе истории после перезапуска IDE). Затем
`gc-acp` задаёт отдельный `session/request_permission` с вариантами «Да, в `edit`», «Да,
в `auto`», «Да, в `manual`» и «Обсудить и доработать»; после ответа карточка вопроса
закрывается (завершена или сорвана, если выбора не было). Выбор режима исполнения меняет
режим сессии, IDE узнаёт об этом до работы продолжения, и агент исполняет план в том же
`session/prompt`. «Обсудить и доработать» оставляет `plan`: следующий запрос человека —
замечания к плану. Без ответа клиента за `acp.confirm_timeout_sec` карточка срывается,
режим не меняется, ничего не исполняется. Чек-лист `session_todo` идёт отдельно и
остаётся `sessionUpdate: "plan"`; план режима `plan` так не отправляется.

## Возможности протокола

Ответ `initialize` (версия протокола фасада — 1):

- загрузка / список / resume / close / delete сессий — да;
- embedded context во входе — да: текстовый `resource` (фрагмент файла
  или selection) склеивается в текст хода; картинки и аудио — нет;
- MCP http/sse через возможности ACP-клиента — нет (`mcpCapabilities` оба `false`).
  Непустой `mcpServers` на `session/new` и `session/load` сессию не рвёт и не
  подключается: предупреждение уходит в stderr. MCP агента из `config.yml`
  работает как обычно, через ядро.

Между кусками ответа клиент показывает имя тула и короткое превью аргументов.

Чек-лист `session_todo` оркестратора уходит клиенту как `session/update` с
`sessionUpdate: "plan"` (полный replace списка; clear — `entries: []`).
Чек-лист субагента в plan сессии не зеркалится. Title сессии — тем же правилом,
что `session/list` (первая строка первой реальной user-реплики): при первом
`session/prompt` и на `session/load` / `session/resume` уходит
`session_info_update`, без обязательного повторного list. В конце хода, если
известны и `last_main_prompt_tokens`, и размер окна модели, шлётся
`usage_update`; иначе тишина. В начале каждого хода, до дельт модели, уходит
`agent_thought_chunk` со строкой активной модели этого хода (алиас и
провайдерский id, если они различаются). Плагин без пикера показывает её в
трейсе рассуждения. В файл треда строка не пишется и при `session/load` не
проигрывается. Служебные technical / сводки компакции в ленту как
`user_message_chunk` не попадают. `notice` / `compaction_*` пока не шлём.

`session/new`, `session/load` и `session/resume` отдают опции `configOptions`: режим
(`id: "mode"`, см. выше) и модель (`id: "model"`): алиасы уровней `default`, `light`, `max`
без повторов, провайдерский id — только в `description`, если он не совпадает
с алиасом. `session/set_config_option` пишет уровень этой сессии в память и действует
со следующего запроса к модели — в том числе внутри уже идущего хода, как во
всех режимах ядра; `config.yml` не меняется.
Пустой реестр или `--model` / `--completions_url` поверх реестра — одна строка,
сменить её нельзя. Соседняя сессия того же процесса остаётся на своём уровне.

Когда автовыбор уровня доступен этому конфигу (условия — в разделе «Включение»
[автороутинга](auto-routing.md#включение)), в пикере после алиасов есть строка
`auto`: выбор пишет сессии уровень `auto`, выбор алиаса возвращает
фиксированный уровень. В `auto` пикер показывает `auto`, а строка трейса —
`Модель: auto`: модель выбирается на каждый запрос. Если `level: auto`, а
автовыбор недоступен, строки `auto` нет, и пикер показывает уровень, которым
ход идёт на деле.

## Пакет

| | |
|---|---|
| Дистрибутив | `gc-acp` (`packages/acp`) |
| Зависит от | `gc-core` |
| Команда | `gc-acp` → `gc_acp.main:main` |
| В мета `gc-ua` | да |

Раскладка дистрибутивов — [packaging.md](packaging.md).

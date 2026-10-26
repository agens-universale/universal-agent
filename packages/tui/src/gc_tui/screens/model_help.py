"""Отдельный экран справки по настройкам модели."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.editors.model_entry_form import KWARGS_HINT
from gc_tui.services.trace_render import build_block_widgets

MODEL_HELP = (
    """# Параметры модели

В каждое поле вводится YAML-словарь `ключ: значение`, без внешней обёртки
`main:`, `auxiliary:` или `backend:`. Параметры тела запроса принимает API
провайдера: одинаковые ключи не гарантируют поддержку всеми моделями.

## Диалог и служебные запросы

"""
    + KWARGS_HINT
    + """

## Наследование между слоями

Базовый конфиг → проектный конфиг → профиль. Например, база задаёт
`main: {temperature: 0.2}`, проект задаёт `main: {top_p: 0.9}`:
проект заменяет весь словарь, температура из базы не добавляется.
Пустое поле в профиле снимает его настройку и открывает проектный слой.
`null` возвращает общий словарь соответствующей роли; `{}` отключает
дополнительные параметры. Auxiliary всегда выбирается у модели фактического
служебного запроса, включая откат на активную модель. К служебным вызовам
относятся сжатие истории, судьи, finish gate, intent scope и черновик роли.

## Рассуждения и история

Параметры reasoning включают генерацию рассуждений на сервере.
`strip_think` управляет сохранением уже полученного `reasoning_content`:
`default` наследует общий флаг, `true` вырезает, `false` сохраняет.
Дефолт кода — сохранять; служебные вызовы всегда вырезают рассуждения.
Этот переключатель не включает и не выключает reasoning API.

## Подключение и лимиты · backend

Поле `backend` содержит только транспортные и лимитные настройки конкретной
модели. Пустое поле или `null` снимает секцию и оставляет общий слой; явный `{}`
отправляет запрос без дополнительных переопределений. В `Responses` транспорт
общий, а `main` и `auxiliary` относятся только к `chat/completions`.

Ключи переопределяются по одному; отсутствующие берутся из общего подключения.
Допустимые ключи и примеры:

`request_timeout_sec` — Таймаут в секундах, положительное целое. Пример: `120`.

`proxy` — HTTP/SOCKS-прокси этого эндпоинта. Пример: `"http://localhost:8080"`.

`use_ssl` — Проверка TLS; строка — путь к набору доверенных сертификатов. Пример: `true`.

`cert_path` — Клиентский сертификат, строка или null. Пример: `"/path/client.pem"`.

`key_path` — Клиентский ключ, строка или null. Пример: `"/path/client.key"`.

`use_retry` — Повторы временных ошибок. Пример: `true`.

`retry_embedded_choice_errors` — Повторы ошибки внутри choice. Пример: `false`.

`chat_completions_max_tokens` — Лимит ответа. Пример: `8192`.

`chat_completions_min_response_tokens` — Минимальный бюджет ответа. Пример: `1024`.

`use_max_tokens_param` — max_completion_tokens; true — max_tokens. Пример: `false`.

`openai_strict_tools_schema` — Строгая схема инструментов. Пример: `true`.

`send_tool_strict` — Передача признака strict. Пример: `false`.

`claude_prompt_cache` — Автовыбор кеширования; true/false — принудительно. Пример: `null`.

`vision` — Запрет изображений; true/null — автоматическая проверка зрения. Пример: `false`.

`chat_completion_headers` — HTTP-заголовки. Пример: `{X-Client: agent}`.

`completions_url` — Адрес API. Пример: `"https://server/v1/chat/completions"`.

Флаги схемы инструментов действуют в chat/completions.
`api_mode`, `stream` и `chat_completion_kwargs` в этом поле не поддерживаются.
Ключи `model`, `messages`, `tools`, `stream`, `max_tokens`,
`max_completion_tokens` в main/auxiliary задаёт рантайм.

## Токен и ссылки на секреты

Обычное поле токена принимает голый ключ, Bearer, Basic и другую явную схему.
Проба, список моделей и рабочие запросы начинают с введённого формата.
Только после HTTP 401 для голого ключа/Bearer проверяется одна альтернатива на том же
задании; вторая картинка остаётся для неподтверждённого ответа.
Остальные схемы передаются как есть; без авторизации заголовок отсутствует.
Для текстового токена проба подставляет принятую форму в поле; она записывается
только при сохранении формы. Ссылки ENV/FILE остаются ссылками.

Можно дополнительно задать заголовок в backend:

```yaml
chat_completion_headers:
  Authorization: ${ENV:MODEL_TOKEN}
request_timeout_sec: 120
```

Token и Authorization в backend одновременно не задаются. Не дублируйте
адрес в поле URL и completions_url. Ссылки `${ENV:NAME}` и `${FILE:/path}`
сохраняются исходными строками; секрет раскрывается для запроса.
Замена ссылки ENV литералом требует подтверждения сохранения.

"""
)


class ModelHelpScreen(Screen):
    """Полноэкранная справка, открываемая из формы модели клавишей F1."""

    DEFAULT_CSS = """
    ModelHelpScreen #model-help-root { height: 1fr; }
    ModelHelpScreen #model-help-header { height: 1; color: $dim; }
    ModelHelpScreen #model-help-body { height: 1fr; padding: 0 1; }
    ModelHelpScreen #model-help-footer { height: 2; color: $dim; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="model-help-root"):
            with Horizontal(id="model-help-header"):
                yield Static("Настройки · Параметры модели", markup=False)
            body = VerticalScroll(id="model-help-body")
            yield body
            yield Static(
                "Esc — назад · ↑/↓ / PgUp/PgDn — прокрутка",
                id="model-help-footer",
                markup=False,
            )

    def on_mount(self) -> None:
        body = self.query_one("#model-help-body", VerticalScroll)
        widgets = build_block_widgets(
            MODEL_HELP, prose_bullet=False, event_no=0
        )
        for widget in widgets:
            widget.can_focus = False
        body.mount(*widgets)
        body.focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.app.pop_screen()

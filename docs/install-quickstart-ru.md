# Установка GigaCode Universal Agent — простой способ

GigaCode Universal Agent (`gc-ua`) — агент для выполнения задач в терминале. Ниже — установка одной командой, без клонирования репозитория.

Перед началом получите у команды GigaCode **адрес сервера** и **токен доступа** — они понадобятся на шаге 4.

---

## Шаг 1. Установить Git и uv (один раз)

**Windows** (PowerShell):
```powershell
winget install Git.Git
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```
**macOS:**
```bash
xcode-select --install
curl -LsSf https://astral.sh/uv/install.sh | sh
```
**Linux (Debian/Ubuntu):**
```bash
sudo apt install -y git
curl -LsSf https://astral.sh/uv/install.sh | sh
```

После установки **перезапустите терминал**.

## Шаг 2. Установить агента

<!-- ВРЕМЕННО: @ветка нужна для установки из ветки (метод-2 пока работает только из неё); после мержа в master убрать "@feat/..." -->
```bash
uv tool install "git+https://gitverse.ru/gigacode-ai/background-agent@feat/agent-instructions-and-gc-guard"
```
В **корпоративной сети Сбера** добавьте в конец `--system-certs` (иначе установка прервётся на сертификатах).

Устанавливаются две команды: **`gc-ua`** — основной режим с текстовым интерфейсом, и **`gc-cli`** — командная строка без интерфейса (разовые запросы, скрипты, автоматизация).

## Шаг 3. Создать конфиг

```bash
gc-ua
```
Первый запуск создаёт файл настроек и **останавливается с подсказкой** — это нормально. Файл настроек:
Windows — `~/.gc/config.yml` (`C:\Users\$USER\.gc\config.yml`); macOS/Linux — `~/.config/gc/config.yml`.

## Шаг 4. Вписать адрес и токен

Откройте `config.yml` в любом редакторе (на Windows — Блокнот), измените:
```yaml
backend:
  completions_url: "ВАШ_АДРЕС/v1/chat/completions"
  use_ssl: true                            # true, если адрес начинается с https://
  chat_completion_headers:
    Authorization: "Bearer ВАШ_ТОКЕН"      # пробел после Bearer сохранить
agent:
  model: "имя_модели"                      # если выдали; иначе не трогайте
```
Сохраните изменения.

## Шаг 5. Запустить

```bash
gc-ua
```

## Шаг 6 (по желанию). MCP-серверы

MCP-серверы — подключаемые расширения, которые добавляют агенту новые возможности (поиск, работа с документами, браузер и т.д.). После установки подключаются автоматически — настройки править не нужно.

```bash
gc-mcp install                     # установить все рекомендованные
gc-mcp install duckduckgo_search   # установить один по имени
gc-mcp list                        # показать доступные и установленные
gc-mcp remove duckduckgo_search    # удалить сервер по имени
gc-mcp remove --all                # удалить все сразу
gc-ua -a                           # запустить агента со всеми установленными серверами
```

| MCP-сервер | Что умеет |
|---|---|
| `duckduckgo_search` | Поиск в интернете — веб-страницы и новости. |
| `office` | Чтение и создание документов Word, Excel, PowerPoint. |
| `science` | Поиск научных публикаций (PubMed, arXiv, Semantic Scholar и др.). |
| `computer` | Автоматизация Windows: управление окнами и элементами интерфейса (только Windows). |
| `browser_playwright` | Управление браузером — открывать сайты, кликать, заполнять формы. |

---

## Если не работает

| Симптом | Решение |
|---|---|
| `git`/`uv` не найдены | Повторите шаг 1 и перезапустите терминал. |
| uv поставился, но команда не найдена | Добавьте `~/.local/bin` в PATH: `source ~/.local/bin/env` |
| Падает на сертификатах | Добавьте `--system-certs` (шаг 2). |
| Ошибка 401 / нет ответа | Проверьте адрес и токен в `config.yml`. |
| Обновить / удалить | `uv tool install … --reinstall` / `uv tool uninstall gc-ua` |

Поддержка: [Телеграм](https://t.me/+2AwDQhHxaAdhNmUy) · [Сберчат](https://sberchat.sberbank.ru/@gc-ua-support).

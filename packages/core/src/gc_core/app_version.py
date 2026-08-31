"""Версия агента и источник установки (direct_url.json, PEP 610).

Технический слой: только чтение метаданных, без бизнес-логики и без print.
uv-receipt.toml сознательно НЕ используется: не содержит коммита (проверено
живьём 2026-07-11), а direct_url.json пишет любой PEP 610-установщик.
"""
from __future__ import annotations

import json
from typing import Callable, NamedTuple, Optional

# Профили установки, в порядке убывания «полноты». Агент ставится одним из
# этих дистрибутивов, и какой именно — определяет и версию, и то, чем
# переустанавливаться при --update. Мета идёт первой: если стоит она, значит
# профиль полный, даже когда рядом лежат её листья.
# gc-mcp стоит ПЕРЕД gc-core, и это не косметика: менеджер рецептов зависит от
# ядра, поэтому на голой установке `gc-mcp` в окружении лежит и gc-core — при
# прежнем порядке перебор доходил сперва до ядра и называл установку «gc-core»,
# а --update переустанавливал не тот дистрибутив. Профили-приложения от вставки
# не страдают: они перечислены выше и находятся раньше, а gc-mcp от них не
# зависит и подменить их не может.
PROFILE_DISTS = ("gc-ua", "gc-tui", "gc-server", "gc-cli", "gc-mcp", "gc-core")

# Подкаталог манифеста каждого профиля в репозитории. Нужен для переустановки:
# uv читает из git только КОРНЕВОЙ pyproject.toml, поэтому лист без
# `#subdirectory=` не зарезолвится (проверено: «Package metadata name `gc-ua`
# does not match given name `gc-tui`»). У меты подкаталога нет и не будет — она
# лежит в корне; словарь поэтому УЖЕ, чем PROFILE_DISTS, ровно на одну запись, и
# install_hint обязан это переживать (см. его докстринг).
PROFILE_SUBDIRS = {
    "gc-core": "packages/core",
    "gc-cli": "packages/cli",
    "gc-tui": "packages/tui",
    "gc-server": "packages/server",
    "gc-mcp": "packages/mcp",
}

# Канонический git-URL репозитория. Живёт рядом с PROFILE_SUBDIRS намеренно:
# подсказки «поставьте профиль» носили подкаталог у себя строкой и разъехались
# при переименовании packaging/ → packages/ — пользователю показывали команду,
# ведущую в несуществующий каталог.
REPO_GIT_URL = "https://gitverse.ru/gigacode-ai/universal-agent"


def install_hint(dist: str) -> str:
    """Команда установки профиля, одной строкой.

    Единственный источник правды для сообщений «профиль не установлен»:
    подкаталог берётся из PROFILE_SUBDIRS, а не переписывается руками.

    Отсутствие подкаталога — не ошибка, а верный случай: мета `gc-ua` лежит в
    корне репозитория, и голый git-URL резолвится именно в неё. Раньше здесь
    стояло `PROFILE_SUBDIRS[dist]`, и естественная форма
    `install_hint(installed_dist())` падала KeyError на самой обычной, ПОЛНОЙ
    установке — `installed_dist()` возвращает как раз `gc-ua`. Не стреляло лишь
    потому, что единственный вызов (gc_tui/__init__.py) шёл с литералом
    "gc-tui".
    """
    subdir = PROFILE_SUBDIRS.get(dist)
    anchor = f"#subdirectory={subdir}" if subdir else ""
    return f'uv tool install --force "{dist} @ git+{REPO_GIT_URL}{anchor}"'


# Совместимость: имя, под которым агент публиковался до разделения профилей.
DIST_NAME = PROFILE_DISTS[0]


class InstallSource(NamedTuple):
    url: str
    ref: Optional[str]      # requested_revision; None = дефолтная ветка источника
    commit: Optional[str]   # зарезолвленный коммит установки
    editable: bool          # dev-установка из исходников
    dist: str = DIST_NAME   # имя установленного дистрибутива-профиля
    subdirectory: Optional[str] = None   # PEP 610: подкаталог манифеста в репо


def installed_dist() -> str:
    """Имя установленного дистрибутива-профиля.

    Листовые профили (gc-tui / gc-server / gc-cli / gc-mcp) живут в подкаталогах
    репозитория, и при их установке дистрибутива `gc-ua` в окружении нет вовсе —
    поэтому имя нельзя зашивать константой. Имена в скобках держались от прежней
    схемы (`gc-ua-tui`, `gc-ua-server`) и врали с 502213be.

    Кэша здесь сознательно нет. Перебор стоит доли миллисекунды на открытие
    /settings, а закэшированное значение залипло бы после `--update` в живом
    процессе: агент до перезапуска показывал бы профиль, которого уже нет.
    """
    from importlib import metadata
    for name in PROFILE_DISTS:
        try:
            metadata.distribution(name)
            return name
        except metadata.PackageNotFoundError:
            continue
    return DIST_NAME


def agent_version() -> str:
    from importlib import metadata
    try:
        return metadata.version(installed_dist())
    except metadata.PackageNotFoundError:
        return "dev"


def is_dev_install() -> bool:
    """Запуск из исходников (editable) либо без метаданных PEP 610.

    В этом случае код может быть новее пакета, и номер версии соврёт — показывать
    его нельзя. Признак один на все экраны: карточка главного и /settings."""
    src = install_source()
    return src is None or src.editable


# Что показывать вместо номера при запуске из исходников. Один текст на все экраны:
# карточка главного и /settings — иначе одно и то же состояние называлось бы
# по-разному.
DEV_LABEL = "dev · установка из исходников"


def display_version(*, long: bool = False) -> str:
    """Версия для показа человеку: «v0.0.1», а из исходников — «dev».

    `long` — для мест, где есть куда развернуться (бренд-карточка, секция
    «Обновление»): там вместо голого «dev» показываем DEV_LABEL. В тесных строках
    (шапка /settings) остаётся короткая форма, но правило «editable → номер не
    показываем» одно и то же: иначе шапка обещала бы версию, которой в коде нет."""
    if is_dev_install():
        return DEV_LABEL if long else "dev"
    return f"v{agent_version()}"


def _read_direct_url_text() -> Optional[str]:
    from importlib import metadata
    try:
        dist = metadata.distribution(installed_dist())
    except metadata.PackageNotFoundError:
        return None
    # read_text возвращает None, если файла нет (importlib сам гасит
    # FileNotFoundError/PermissionError); except — страховка от экзотики ФС.
    try:
        return dist.read_text("direct_url.json")
    except OSError:
        return None


def install_source(
    _read: Callable[[], Optional[str]] = _read_direct_url_text,
) -> Optional[InstallSource]:
    """Источник установки или None (нет direct_url.json / нечитаем)."""
    text = _read()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    url = data.get("url") or ""
    if not url:
        return None
    vcs = data.get("vcs_info") or {}
    dir_info = data.get("dir_info") or {}
    return InstallSource(
        url=url,
        ref=vcs.get("requested_revision"),
        commit=vcs.get("commit_id"),
        editable=bool(dir_info.get("editable")),
        dist=installed_dist(),
        # PEP 610 кладёт subdirectory верхним ключом. У меты его нет (она в
        # корне репозитория), у листьев — путь к их манифесту; без него
        # переустановка листа не зарезолвится: uv читает из git только корневой
        # pyproject.toml.
        subdirectory=data.get("subdirectory"),
    )

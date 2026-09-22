"""Единый резолвер корневых ассетов (prompts/, skills/, config.default.yml).

Ассеты принадлежат дистрибутиву gc-core: фронтенды живут в других
дистрибутивах и дотягиваются до них через этот же резолвер, потому что он
считает путь от СВОЕГО ``__file__``, а не от рабочего каталога.

Технический слой — без бизнес-логики.
"""

import re
from functools import lru_cache
from pathlib import Path

_MARKER = "prompts"
# Насколько глубоко подниматься в поисках packages/core/assets. Реальная
# глубина — 2 (gc_core → src → core), запас на случай иной раскладки.
_MAX_UP = 4


def bundled_root() -> Path:
    """Каталог, содержащий prompts/ (маркер) и прочие bundled-ассеты.

    Две раскладки, обе поддерживаются:

    - wheel: ``site-packages/gc_core/bundled_assets.py`` → ассеты лежат ВНУТРИ
      пакета, в ``gc_core/assets``;
    - репозиторий: ``packages/core/src/gc_core/bundled_assets.py`` → ассеты
      лежат в ``packages/core/assets`` — поднимаемся до каталога, у которого
      есть ``assets/prompts``.

    Внутрь пакета ассеты переехали 18.08.2026. До этого в колесе они лежали
    РЯДОМ с ним, прямо в ``site-packages``, и дистрибутив занимал там четыре
    родовых имени верхнего уровня. В Python 3 любой каталог на ``sys.path`` —
    namespace-пакет, поэтому у встраивающего приложения ``import prompts``
    молча резолвился в наши промпты (проверено в чистом venv).
    """
    here = Path(__file__).resolve().parent
    # Внутри пакета — основной случай и в колесе, и в рабочей копии после
    # переезда ассетов.
    if (here / "assets" / _MARKER).is_dir():
        return here / "assets"
    # Совместимость: старые колёса, где ассеты лежали рядом с пакетом.
    for cand in (here, here.parent):
        if (cand / _MARKER).is_dir():
            return cand
    for up in here.parents[:_MAX_UP]:
        cand = up / "assets"
        if (cand / _MARKER).is_dir():
            return cand
    raise FileNotFoundError(
        f"bundled assets not found: {_MARKER}/ не найден ни в {here / 'assets'}, "
        f"ни рядом с {here}, ни в assets/ выше по дереву (ожидается "
        f"gc_core/assets в колесе или packages/core/assets в репозитории)"
    )


#: Строгий маркер рабочей копии: `packages/core/pyproject.toml`, объявляющий
#: `name = "gc-core"`. Прежний маркер — «каталог packages/ рядом с
#: pyproject.toml» — ловил ЛЮБОЕ чужое монорепо: в проекте с `packages/*/src`
#: и нашими колёсами в его .venv repo_root() возвращал ЕГО корень (сервер брал
#: чужой config_server.yml, установщик MCP искал external/vendor в чужом
#: дереве). tomllib не используется намеренно: проверяется ровно одна строка, а
#: копии этой функции в gc_testkit и recommended_mcp обязаны совпадать с ней
#: дословно — при том что gc-testkit объявляет requires-python >= 3.10, где
#: tomllib ещё нет.
_GC_CORE_NAME = re.compile(r'^[ \t]*name[ \t]*=[ \t]*["\']gc-core["\'][ \t]*$', re.MULTILINE)


def _declares_gc_core(candidate: Path) -> bool:
    """Лежит ли в `candidate` наш `packages/core/pyproject.toml`."""
    try:
        text = (candidate / "packages" / "core" / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _GC_CORE_NAME.search(text) is not None


def repo_root_from(start: Path) -> Path | None:
    """Корень РАБОЧЕЙ КОПИИ вверх от `start`, иначе None.

    Отдельный вход от :func:`repo_root` нужен тем, кто ищет корень не от своего
    ``__file__``, а от переданного каталога: адаптер Harbor резолвит промпты от
    каталога адаптеров.

    Это КАНОНИЧЕСКАЯ реализация маркера. Две её копии — в
    ``recommended_mcp.runtime.repo_path`` и ``gc_testkit.paths`` — оставлены
    осознанно: обе работают ДО того, как gc_core попадает на ``sys.path``
    (прямой запуск ``install.py`` только этим и занят, а плагин тест-кита сам
    кладёт исходники на путь), поэтому импортировать отсюда им нечем.
    Совпадение трёх копий стережёт ``tests/test_repo_root_marker.py``.
    """
    for parent in Path(start).resolve().parents:
        if _declares_gc_core(parent):
            return parent
    return None


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """Корень РАБОЧЕЙ КОПИИ репозитория, если код запущен из неё, иначе None.

    Не путать с :func:`bundled_root`. До разбиения на пакеты ассеты лежали в
    корне репозитория, поэтому ``bundled_root()`` возвращал сам корень, и на нём
    держались вещи, которые к ассетам отношения не имеют: `config_server.yml`,
    `scripts/*.py`, обёртка swarm. После переезда ассетов в
    ``packages/core/assets`` смысл функции тихо поменялся, и все такие пути
    поехали в несуществующий ``assets/scripts/…`` — молча, потому что рядом не
    было ни одной проверки существования.

    В установленном колесе рабочей копии нет, поэтому здесь честный ``None``:
    вызывающий обязан решить, что делать (взять cwd, взять переменную
    окружения, отказаться), а не получить путь, которого не существует.
    """
    return repo_root_from(Path(__file__))

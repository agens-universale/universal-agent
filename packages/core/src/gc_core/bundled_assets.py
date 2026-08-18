"""Единый резолвер корневых ассетов (prompts/, skills/, config.default.yml).

Ассеты принадлежат дистрибутиву gc-core: фронтенды живут в других
дистрибутивах и дотягиваются до них через этот же резолвер, потому что он
считает путь от СВОЕГО ``__file__``, а не от рабочего каталога.

Технический слой — без бизнес-логики.
"""
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


def repo_root() -> Path | None:
    """Корень РАБОЧЕЙ КОПИИ репозитория, если код запущен из неё, иначе None.

    Не путать с :func:`bundled_root`. До разбиения на пакеты ассеты лежали в
    корне репозитория, поэтому ``bundled_root()`` возвращал сам корень, и на нём
    держались вещи, которые к ассетам отношения не имеют: `config_server.yml`,
    `scripts/*.py`, обёртка swarm. После переезда ассетов в
    ``packages/core/assets`` смысл функции тихо поменялся, и все такие пути
    поехали в несуществующий ``assets/scripts/…`` — молча, потому что рядом не
    было ни одной проверки существования.

    Маркер — каталог ``packages/`` рядом с ``pyproject.toml``. В установленном
    колесе рабочей копии нет, поэтому здесь честный ``None``: вызывающий обязан
    решить, что делать (взять cwd, взять переменную окружения, отказаться), а не
    получить путь, которого не существует.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "packages").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
    return None

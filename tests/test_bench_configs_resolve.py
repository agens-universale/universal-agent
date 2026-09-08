"""Конфиги замеров ссылаются на существующие промпты.

Что было. `integrations/bench_configs/*.yml` держали пути вида `../prompts/…`.
Пока `bench_configs/` лежал в корне репозитория, а `prompts/` — рядом с ним, это
было верно. Разбиение переселило и то, и другое: конфиги уехали в
`integrations/`, промпты — в `packages/core/assets/`, и `../prompts/…` стал
указывать в `<repo>/integrations/prompts/`, где ничего нет.

Ни один тест этого не ловил, ссылок на конфиги в дереве нет — падало бы только у
человека, который запустил замер руками. Причём не пустым ответом, а `OSError` до
первого шага: у `chat_completions_system_prompt_path` бандлового фолбэка нет.

Сетка смотрит на РЕЗОЛВ, а не на текст: пути относительны каталогу конфига, и
проверять их подстрокой значит проверять не то.
"""
from __future__ import annotations

import os

import pytest

from gc_core.config import load_config_ex
from gc_testkit.paths import REPO

BENCH_DIR = REPO / "integrations" / "bench_configs"

#: Конфиги АГЕНТА — по одному на комбинацию «сводка × субагенты».
CONFIGS = sorted(BENCH_DIR.glob("0*.yml"))

#: Остальные файлы каталога конфигами агента НЕ являются: это определения
#: прогонов для harbor, у них своя схема (`gc_cli_config`, `model_served`,
#: `harbor`), и `load_config_ex` отвергает их как `extra_forbidden`. Список
#: поимённый, а не «всё, что не 0*»: новый файл обязан попасть под сетку либо
#: сюда — то есть решением, а не молча.
NOT_AGENT_CONFIGS = {"bench_config.yml", "bench_config_dsv4_v0.2.3.yml"}


def _markdown_paths(node, prefix=""):
    """Все значения-пути к .md в разобранном конфиге, с их ключами."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _markdown_paths(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, str) and node.endswith(".md"):
        yield prefix, node


def test_bench_configs_exist():
    """Страховка от вакуума: без конфигов сетка ниже зелена и бесполезна."""
    assert CONFIGS, f"в {BENCH_DIR} не найдено ни одного конфига замеров"


def test_every_bench_file_is_accounted_for():
    """Каждый файл каталога либо судится сеткой, либо назван исключением.

    Иначе конфиг с битыми путями появится под именем, не подходящим под `0*`, и
    сетка его не заметит — ровно тот способ, которым дефект и дожил до ревью.
    """
    seen = {p.name for p in CONFIGS} | NOT_AGENT_CONFIGS
    actual = {p.name for p in BENCH_DIR.glob("*.yml")}
    assert actual <= seen, (
        f"новые файлы в {BENCH_DIR.name}, не отнесённые ни к конфигам агента, ни "
        f"к определениям harbor: {sorted(actual - seen)}"
    )


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.stem)
def test_bench_config_prompts_exist(config):
    """Каждый .md-путь конфига резолвится в существующий файл.

    Подмена, которую ловит тест: вернуть `../prompts/` вместо
    `../../packages/core/assets/prompts/`.
    """
    cfg, _meta = load_config_ex(str(config))

    found = list(_markdown_paths(cfg))
    assert found, f"{config.name}: в конфиге нет ни одного пути к промпту"

    missing = [f"{key} → {path}" for key, path in found if not os.path.isfile(path)]
    assert not missing, (
        f"{config.name}: промпты не найдены —\n  " + "\n  ".join(missing)
    )

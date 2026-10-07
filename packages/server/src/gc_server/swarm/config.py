import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

from gc_core.bundled_assets import bundled_root
from gc_core.config_modules import env_vars

# Корень bundled-ассетов. Раньше брался из rest_api.server_config — это была
# единственная зависимость swarm → server, причём там PROJECT_ROOT объявлен как
# тот же str(bundled_root()) над ядром. Импортируем напрямую, чтобы swarm
# зависел только от core.
PROJECT_ROOT = str(bundled_root())

# Каталог самого пакета: там лежит обёртка песочницы (package data). Держим
# отдельно от корня ассетов — обёртка это код дистрибутива, а не промпт.
_PACKAGE_DIR = str(Path(__file__).resolve().parent)

logger = logging.getLogger(__name__)


def _resolve_asset_path(
    maybe_path: Any, default_rel: str, *, base: str | None = None
) -> str:
    """
    Путь к файлу, который ЕДЕТ В КОЛЕСЕ вместе с дистрибутивом.

    - Если значение пустое или не строка — использовать ``base``/``default_rel``.
    - Если путь абсолютный — вернуть как есть.
    - Если путь относительный — резолвится от ``base``.

    ``base`` по умолчанию — корень ассетов. Для файлов самого дистрибутива
    (обёртка песочницы) вызывающий передаёт каталог пакета явно.

    Такой якорь верен ровно для этих двух полей (agentmd_path, wrapper_path):
    их цель лежит ВНУТРИ установленного пакета. Всё остальное, что человек
    пишет в config_server.yml, резолвит `_resolve_user_path` — см. его докстринг.
    """
    root = base or PROJECT_ROOT
    if not isinstance(maybe_path, str) or not maybe_path.strip():
        return os.path.join(root, default_rel)

    path = maybe_path.strip()
    if os.path.isabs(path):
        return path
    return os.path.join(root, path)


def _resolve_wrapper_path(maybe_path: Any, config_dir: str) -> str:
    """Путь к обёртке песочницы.

    Пусто → обёртка из пакета. Абсолютный → как есть. Относительный ищется
    сначала рядом с `config_server.yml`, затем в каталоге пакета; если не нашёлся
    нигде — берётся обёртка из пакета, громко.

    Почему не просто якорь: на develop `bundled_root()` был корнем репозитория, и
    `demo_config_server.yml` раскладывал штатным значением
    `"scripts/run-cli-in-sandbox.sh"` — то есть `<репозиторий>/scripts/...`.
    Обёртка с тех пор переехала внутрь пакета (в `scripts/` она не попадала ни в
    одно колесо, дефолта там быть не могло), а якорь для относительных путей стал
    каталогом пакета. У всех, кто скопировал шаблон с develop, значение
    превращалось в несуществующий `gc_server/swarm/scripts/run-cli-in-sandbox.sh`,
    и КАЖДЫЙ `POST /swarm/run` отвечал 500 — при живой обёртке в соседнем файле.

    Поиск рядом с конфигом идёт первым: в рабочей копии `config_server.yml` лежит
    в корне репозитория, поэтому старое значение снова означает ровно то же, что
    на develop. Фолбэк на пакетную обёртку спасает установленный профиль, где
    `scripts/` не существует в принципе.
    """
    default = os.path.join(_PACKAGE_DIR, "run-cli-in-sandbox.sh")
    if not isinstance(maybe_path, str) or not maybe_path.strip():
        return default

    path = maybe_path.strip()
    if os.path.isabs(path):
        return path

    local = os.path.normpath(os.path.join(config_dir, path))
    if os.path.isfile(local):
        return local
    packaged = os.path.normpath(os.path.join(_PACKAGE_DIR, path))
    if os.path.isfile(packaged):
        return packaged

    logger.warning(
        "swarm.wrapper_path=%r не найден (искал %s и %s) — беру обёртку из пакета: %s",
        maybe_path,
        local,
        packaged,
        default,
    )
    return default


def _resolve_user_path(
    maybe_path: Any, default: str, *, config_dir: str
) -> str:
    """
    Путь, который человек написал в своём config_server.yml.

    - Абсолютный — как есть.
    - Относительный — от каталога САМОГО config_server.yml, то есть от того
      места, где человек этот путь и видит.
    - Пустой/не строка — ``default`` (вызывающий передаёт уже абсолютный путь;
      "" означает «поле опционально, дефолт считает кто-то ниже»).

    Раньше здесь стоял тот же якорь, что у пакетных файлов, — корень ассетов.
    На develop это совпадало с корнем репозитория (`bundled_root()` возвращал
    `here.parent`), рядом с которым и лежит config_server.yml, поэтому
    `swarm_logs_dir: "./agent_logs"` давал каталог рядом с конфигом. После
    переезда ассетов внутрь пакета тот же якорь стал указывать в
    site-packages/gc_core/assets: логи агентов писались в чужой пакет и
    стирались при `uv tool upgrade`, а несуществующий `<assets>/swarm_skills`
    молча оставлял песочницу без скиллов.
    """
    if not isinstance(maybe_path, str) or not maybe_path.strip():
        return default
    path = maybe_path.strip()
    return path if os.path.isabs(path) else os.path.join(config_dir, path)


def _user_config_dir(server_cfg: Dict[str, Any], explicit: str | None) -> str:
    """Каталог, от которого резолвятся относительные пути пользовательского конфига.

    Приоритет: явный аргумент `config_dir` → каталог прочитанного
    config_server.yml (ключ `config_path`, его кладёт `load_server_config`) →
    CWD. CWD в хвосте не наугад: это первый кандидат в поиске самого
    config_server.yml (см. `rest_api.server_config._server_config_candidates`),
    так что при «файла нет вовсе» относительный путь смотрит туда же, куда
    смотрел поиск.
    """
    if isinstance(explicit, str) and explicit.strip():
        return os.path.abspath(explicit.strip())
    cfg_path = server_cfg.get("config_path")
    if isinstance(cfg_path, str) and cfg_path.strip():
        return os.path.dirname(os.path.abspath(cfg_path.strip()))
    return os.getcwd()


def build_swarm_config(
    server_cfg: Dict[str, Any], *, config_dir: str | None = None
) -> Dict[str, Any]:
    """
    Построить нормализованный конфиг для swarm-режима.

    На вход подаётся целиком SERVER_CONFIG (как в src.server),
    ``config_dir`` — каталог, от которого резолвятся ОТНОСИТЕЛЬНЫЕ пути
    пользовательского конфига. Обычно передавать его не нужно:
    `load_server_config` кладёт прочитанный путь в `server_cfg["config_path"]`,
    и каталог берётся оттуда (см. `_user_config_dir`).

    На выходе — словарь с полями:
      - enabled: bool
      - threads_dir: str (абсолютный путь)
      - sandbox_root_dir: str (абсолютный путь)
      - max_concurrent: int
      - ttl_seconds: int
      - limits: Dict[str, int]
      - cli_command: str
      - wrapper_path: str (абсолютный путь)
      - sandbox_tool: str
      - agentmd_path: str (абсолютный путь)
      - skills_dir: str (абсолютный путь)
      - skills_dir_configured: bool — путь назван человеком, а не взят дефолтом
    """
    raw_swarm = server_cfg.get("swarm") or {}

    enabled = bool(raw_swarm.get("enabled", False))

    base_dir = _user_config_dir(server_cfg, config_dir)
    paths = env_vars.gc_paths()
    slug = env_vars.project_slug()
    swarm_root = paths["state"] / "projects" / slug / "swarm"
    # Процессный каталог sworm'а (meta/completion/stdout/stderr). Поле в конфиге —
    # `swarm_threads_dir` (старое имя `threads_dir` ещё читается для совместимости).
    threads_dir = _resolve_user_path(
        raw_swarm.get("swarm_threads_dir", raw_swarm.get("threads_dir")),
        str(swarm_root / "threads"),
        config_dir=base_dir,
    )
    sandbox_root_dir = _resolve_user_path(
        raw_swarm.get("sandbox_root_dir"),
        str(swarm_root / "sandboxes"),
        config_dir=base_dir,
    )

    # Куда на хосте lifecycle сохраняет агентские .gc/logs из песочницы при
    # финализации. Абсолютный путь — как есть; относительный — от каталога
    # config_server.yml; пусто → дефолт `threads_dir/<swarm_id>/agent_logs/`.
    swarm_logs_dir = _resolve_user_path(
        raw_swarm.get("swarm_logs_dir"), "", config_dir=base_dir
    )

    max_concurrent = int(raw_swarm.get("max_concurrent", 10))
    ttl_seconds = int(raw_swarm.get("ttl_seconds", 86400))

    limits_raw = raw_swarm.get("limits") or {}
    limits = {
        "memory_mb": int(limits_raw.get("memory_mb", 1024)),
        "disk_mb": int(limits_raw.get("disk_mb", 2048)),
        "cpu_percent": int(limits_raw.get("cpu_percent", 50)),
        "timeout_seconds": int(limits_raw.get("timeout_seconds", 1800)),
    }

    cli_command = str(raw_swarm.get("cli_command", "gc-cli"))
    # Обёртка песочницы лежит ВНУТРИ пакета и едет в колесе как package data,
    # поэтому дефолт работает и в установленном профиле. Раньше она жила в
    # scripts/ репозитория, который не попадал ни в одно колесо: дефолтного
    # пути там быть не могло в принципе.
    wrapper_path = _resolve_wrapper_path(
        raw_swarm.get("wrapper_path"), base_dir
    )
    sandbox_tool = str(raw_swarm.get("sandbox_tool", "bubblewrap"))
    agentmd_path = _resolve_asset_path(
        raw_swarm.get("agentmd_path"), "prompts/server_agent.md"
    )
    # Скиллы для песочницы — пользовательский контент, а не ассет дистрибутива:
    # дефолт кладём рядом с обычным пользовательским слоем скиллов ядра
    # (`gc_paths()["data"]/skills`, см. tools/skills/registry.get_skills_registry),
    # только под своим именем. Каталог переживает `uv tool upgrade`, в отличие
    # от прежнего `<assets>/swarm_skills`, которого к тому же не существовало
    # нигде в дереве — runner молча пропускал копирование.
    skills_dir = _resolve_user_path(
        raw_swarm.get("skills_dir"),
        str(paths["data"] / "swarm_skills"),
        config_dir=base_dir,
    )
    # Отличаем «пользователь назвал каталог» от «взят дефолт». Нужно ровно для
    # громкости предупреждения в runner: несуществующий каталог, который человек
    # выписал руками, — ошибка конфигурации и требует крика; несуществующий
    # дефолт означает просто «swarm-скиллы не заводили», и кричать об этом на
    # КАЖДОМ запуске нельзя. Дефолт непустой всегда, поэтому по самому значению
    # эти два случая уже неразличимы.
    skills_dir_configured = bool(
        str(raw_swarm.get("skills_dir") or "").strip()
    )

    # Шаг 1.11 (AR-6, AR-7): bind-mount пути для wrapper.sh.
    # cli_venv_root и agent_dep_dir принимают только абсолютные пути.
    #
    # Незаданный cli_venv_root дефолтится на venv, в котором работает САМ сервер,
    # если искомая команда там есть. Причина: `uv tool install gc-server` ставит
    # gc-cli зависимостью в venv инструмента, но наружу, в PATH, выносит только
    # gc-serve — команды зависимостей uv не экспортирует. PATH-fallback обёртки
    # в такой установке команду не находил, и КАЖДЫЙ sworm завершался exit 127.
    # А venv сервера — ровно то место, где gc-cli лежит: и при `uv tool install`,
    # и в dev-чекауте с editable-установкой.
    #
    # Если команды там нет (сервер поднят системным python без venv), остаётся
    # прежний PATH-fallback, и обёртка отказывает на нём с внятным сообщением
    # вместо голого exit 127.
    cli_venv_root = _abs_or_empty(raw_swarm.get("cli_venv_root"))
    if (
        # Именно ОТСУТСТВИЕ ключа, а не пустой результат `_abs_or_empty`. Если
        # человек ключ написал, но значение негодное (относительный путь), он
        # обязан увидеть прежнее «игнорируется», а не тихую подстановку чужого
        # venv вместо своей опечатки.
        "cli_venv_root" not in raw_swarm
        # Абсолютный cli_command несовместим с подстановкой: os.path.join
        # выбрасывает всё, что левее абсолютного пути, и проверка вырождается в
        # «существует ли сам этот файл». Он существует — условие проходит, а
        # обёртка потом склеивает `<venv>/bin//usr/local/bin/gc-cli` и не находит
        # ничего. Человек, указавший полный путь, уже сказал, где команда: ему
        # нужна PATH-ветка, а не наш venv.
        and not os.path.isabs(cli_command)
        # Только настоящий venv. Системный или conda-префикс сюда пускать нельзя:
        # обёртка делает `--ro-bind $SWARM_CLI_VENV_ROOT` и молча уехало бы всё
        # дерево префикса ВНУТРЬ изолированной песочницы — ровно то, от чего
        # песочница и заводилась. У venv sys.prefix отличается от base_prefix;
        # установка через `uv tool install` этому удовлетворяет.
        and sys.prefix != sys.base_prefix
        and os.path.isfile(os.path.join(sys.prefix, "bin", cli_command))
    ):
        cli_venv_root = sys.prefix
        logger.info(
            "swarm: cli_venv_root не задан — беру venv сервера %s (он же уйдёт --ro-bind внутрь песочницы)",
            cli_venv_root,
        )
    agent_dep_dir = _abs_or_empty(raw_swarm.get("agent_dep_dir"))
    extra_mounts_raw = raw_swarm.get("extra_mounts") or []
    if not isinstance(extra_mounts_raw, list):
        extra_mounts_raw = []
    extra_mounts = [
        str(p).strip()
        for p in extra_mounts_raw
        if isinstance(p, str) and p.strip()
    ]

    # agent_overrides пробрасываем из server_cfg как есть — materializer применит
    # их к raw YAML с auto-wrap'ом flat → {agent: ...}, чтобы единая секция
    # config_server.yml::agent_overrides работала и для webhook'а (cfg.update
    # на flat-dict) и для swarm'а (deep_merge в raw YAML).
    agent_overrides_raw = server_cfg.get("agent_overrides") or {}
    agent_overrides = (
        dict(agent_overrides_raw)
        if isinstance(agent_overrides_raw, dict)
        else {}
    )

    return {
        "enabled": enabled,
        "threads_dir": threads_dir,
        "sandbox_root_dir": sandbox_root_dir,
        "swarm_logs_dir": swarm_logs_dir,
        "max_concurrent": max_concurrent,
        "ttl_seconds": ttl_seconds,
        "limits": limits,
        "cli_command": cli_command,
        "wrapper_path": wrapper_path,
        "sandbox_tool": sandbox_tool,
        "agentmd_path": agentmd_path,
        "skills_dir": skills_dir,
        "skills_dir_configured": skills_dir_configured,
        "cli_venv_root": cli_venv_root,
        "agent_dep_dir": agent_dep_dir,
        "extra_mounts": extra_mounts,
        "agent_overrides": agent_overrides,
    }


def _abs_or_empty(value: Any) -> str:
    """Принять только непустую строку с абсолютным путём; иначе вернуть ''."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    if not os.path.isabs(candidate):
        return ""
    return candidate


__all__ = ["build_swarm_config"]

"""Конформанс хранилища тредов: проверка чужой реализации одной функцией.

Зачем отдельный модуль, а не тесты в ядре. Реализацию хранилища пишет
ВСТРАИВАЮЩИЙ — у себя, поверх своей базы, в своём репозитории. Ему нужно уметь
проверить её нашей меркой, не переписывая наши тесты. Отсюда публичный вход::

    from gc_testkit.thread_store_conformance import assert_conforms

    def test_my_postgres_store():
        assert_conforms(lambda: PostgresThreadStore(dsn=TEST_DSN))

Почему не ``isinstance``. Ошибка симметрична в обе стороны. Наследование
``ThreadStoreBase`` НЕ обязательно — законное прокси-хранилище на
``__getattr__`` его не пройдёт, хотя работает; а пройденная проверка ничего не
доказывает — наследник с одиннадцатью заглушками ``pass`` тоже наследник.
Раньше здесь стоял ``runtime_checkable`` ``Protocol``, и он был не лучше:
сверял ТОЛЬКО наличие атрибутов, ни сигнатур, ни корутинности. Проверено
запуском — у класса, где все одиннадцать методов ``async def``, ``isinstance``
был истинным, структурный тест зелёным, а восемь поведенческих красными.
Единственный честный ответ на вопрос «годится ли хранилище» — прогнать его.

Почему проверок больше, чем было. Первая редакция контракта описывала методы,
но не свойства — и две НАШИ СОБСТВЕННЫЕ реализации разошлись в четырёх местах,
оставаясь зелёными на всём наборе:

* третий элемент ``load_thread_state`` — количество против номера ``seq``;
* ``known_keys`` пополнялся всем содержимым вместо записанного;
* запись трассы — четыре поля против шести;
* ``id`` на чтении проставляла только одна из двух.

Каждое из четырёх теперь здесь. Правило простое: расходятся реализации ровно
там, где их проверяют по-разному, поэтому проверка одна на всех.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List

#: Одиннадцать методов контракта — реэкспорт из ядра для наборов-конформансов.
from gc_core.thread_store_base import CONTRACT_METHODS as CONTRACT_METHODS

SYS = "cli"

#: Шесть полей записи трассы. Форма замкнутая: её пишет и читает только ядро.
TRACE_FIELDS = frozenset(
    {"ts", "seq", "parent_call_id", "child_run_id", "kind", "payload"}
)


class ThreadStoreContractError(AssertionError):
    """Реализация не удовлетворяет контракту. Текст называет, чем именно."""


def _msg(mid: str, text: str = "текст") -> Dict[str, Any]:
    return {"id": mid, "role": "user", "content": text}


def _fail(what: str, detail: str) -> None:
    raise ThreadStoreContractError(f"{what}\n  {detail}")


# --- структура -------------------------------------------------------------


def check_methods_present(store: Any) -> None:
    """Все одиннадцать методов есть и вызываемы.

    Ловит естественную ошибку встраивающего, которую ``isinstance`` пропускает
    молча: имена с приставкой ``a`` (``aload_meta``, ``aappend_messages``) —
    так называет методы тот, кто прочитал документацию про async и решил, что
    ядро ждёт их. Такой store падает ``AttributeError`` глубоко внутри чтения
    треда, где причина уже не видна.
    """
    missing = [
        m for m in CONTRACT_METHODS if not callable(getattr(store, m, None))
    ]
    if not missing:
        return
    hint = ""
    a_named = [m for m in missing if callable(getattr(store, f"a{m}", None))]
    if a_named:
        hint = (
            f"\n  У вас есть {', '.join('a' + m for m in a_named)} — это имена"
            "\n  АСИНХРОННОГО контракта (gc_core.thread_store_async.AsyncThreadStore)."
            "\n  Синхронный контракт ждёт имена без приставки."
        )
    _fail(
        f"{type(store).__name__}: нет методов контракта: {', '.join(missing)}",
        f"нужны все {len(CONTRACT_METHODS)}: {', '.join(CONTRACT_METHODS)}{hint}",
    )


def check_methods_are_sync(store: Any) -> None:
    """Ни один метод не объявлен ``async def``.

    Главная проверка набора. Асинхронную реализацию ядро вызвать не может:
    двенадцать из шестнадцати обращений лежат в обычных ``def``, где корутина
    молча отбрасывается — без ошибки, без лога, с потерей записи. Падает при
    этом лишь часть путей, поэтому без гейта поломка выглядит как работающий
    агент с пустой трассой субагентов и незагорающимися индикаторами.
    """
    # inspect, а не asyncio: asyncio.iscoroutinefunction объявлена устаревшей
    # (удаление в 3.16), ответы на всех значимых случаях совпадают.
    bad = [
        m
        for m in CONTRACT_METHODS
        if inspect.iscoroutinefunction(getattr(store, m, None))
    ]
    if not bad:
        return
    _fail(
        f"{type(store).__name__}: методы объявлены async def: {', '.join(bad)}",
        "Синхронное хранилище обязано быть синхронным: ядро зовёт его из"
        "\n  обычных def, и корутина там отбрасывается БЕЗ ОШИБКИ — запись"
        "\n  теряется молча."
        "\n  Асинхронному клиенту (redis.asyncio, asyncpg) предназначен"
        "\n  gc_core.thread_store_async.AsyncThreadStore — ядро ждёт его нативно.",
    )


def check_signatures(store: Any) -> None:
    """Имена и вид параметров совпадают с контрактом.

    Сверяем с самим ``ThreadStoreBase``, а не со списком в прозе: разъехаться
    прозе и коду проще, чем коду и коду. Раньше здесь стоял ``ThreadStore``
    (``Protocol``) — он снят, чтобы контракт был объявлен ровно один раз;
    сигнатуры у ABC те же.
    """
    from gc_core.thread_store_base import ThreadStoreBase

    for name in CONTRACT_METHODS:
        want = inspect.signature(getattr(ThreadStoreBase, name))
        got = inspect.signature(getattr(store, name))
        want_p = [p for p in want.parameters.values() if p.name != "self"]
        got_p = list(got.parameters.values())
        if [(p.name, p.kind) for p in want_p] != [
            (p.name, p.kind) for p in got_p
        ]:
            _fail(
                f"{type(store).__name__}.{name}: сигнатура разошлась с контрактом",
                f"контракт: ({', '.join(str(p) for p in want_p)})\n  у вас:    ({', '.join(str(p) for p in got_p)})",
            )


# --- поведение -------------------------------------------------------------


def check_missing_thread_is_empty(store: Any) -> None:
    """Нет треда — пустота, а не исключение."""
    if store.load_messages(SYS, "no-such-thread") != []:
        _fail("load_messages на отсутствующем треде", "ожидался пустой список")
    if store.load_meta(SYS, "no-such-thread") != {}:
        _fail("load_meta на отсутствующем треде", "ожидался пустой словарь")


def check_roundtrip(store: Any) -> None:
    """Записали — прочитали, в порядке записи."""
    written = store.append_messages(
        SYS, "t-round", [_msg("m1", "раз"), _msg("m2", "два")]
    )
    if written != 2:
        _fail(
            "append_messages вернул не число записанных",
            f"вернул {written!r}, ждали 2",
        )
    got = [m["content"] for m in store.load_messages(SYS, "t-round")]
    if got != ["раз", "два"]:
        _fail("история вернулась не в порядке записи", f"вернулось {got!r}")


def check_idempotent(store: Any) -> None:
    """Повтор батча не задваивает историю.

    Не теоретическое требование: батч персистится ДВАЖДЫ — сразу после
    исполнения тулов и ещё раз на выходе цикла. Реализация без дедупликации
    удваивает историю треда на каждом ходу.
    """
    batch = [_msg("m1", "раз"), _msg("m2", "два")]
    store.append_messages(SYS, "t-idem", batch)
    again = store.append_messages(SYS, "t-idem", batch)
    if again != 0:
        _fail("повтор батча записал снова", f"вернул {again!r}, ждали 0")
    n = len(store.load_messages(SYS, "t-idem"))
    if n != 2:
        _fail(
            "повтор батча задвоил историю", f"в треде {n} сообщений, ждали 2"
        )


def check_known_keys_mutated_in_place(store: Any) -> None:
    """``known_keys`` мутируется на месте и ТОЛЬКО записанным.

    Оба свойства нужны вызывающему: он держит тот же объект и снимает снимок
    ДО вызова, чтобы по разнице понять, что дописать в свою историю.
    Защитная копия — естественное решение реализатора — задваивает историю;
    досыпанные «заодно» ключи ломают ту же разницу с другой стороны.
    """
    keys: set = set()
    store.append_messages(SYS, "t-keys", [_msg("k1")], known_keys=keys)
    if not keys:
        _fail(
            "known_keys не пополнился после записи",
            "набор обязан мутироваться НА МЕСТЕ: вызывающий держит тот же объект"
            "\n  и по его приросту понимает, что было записано. Защитная копия"
            "\n  молча задваивает историю на следующем батче.",
        )
    store.append_messages(SYS, "t-keys", [_msg("k2")], known_keys=keys)
    if len(keys) != 2:
        _fail(
            "known_keys пополнился не только записанным",
            f"после двух батчей по одному сообщению в наборе {len(keys)} ключей,"
            "\n  ждали 2. Досыпать туда всё содержимое хранилища нельзя.",
        )


def check_seq_is_a_number_not_a_count(store: Any) -> None:
    """Третий элемент ``load_thread_state`` — НОМЕР, а не количество.

    Расходятся они после ``discard_message``: количество падает вместе с
    удалением, номер остаётся номером последнего выданного. Проверка стоит
    отдельно, потому что оба значения — ``int``, и ни одна проверка типов
    подмену не увидит. Наши собственные реализации разошлись именно здесь.
    """
    store.append_messages(SYS, "t-seq", [_msg("s1"), _msg("s2"), _msg("s3")])
    state = store.load_thread_state(SYS, "t-seq")
    if int(state[2]) != 3:
        _fail(
            "номер seq после трёх записей", f"вернулось {state[2]!r}, ждали 3"
        )

    store.discard_message(SYS, "t-seq", "s2")
    after = store.load_thread_state(SYS, "t-seq")
    if len(after[0]) != 2:
        _fail(
            "discard не убрал сообщение", f"в треде {len(after[0])} сообщений"
        )
    if int(after[2]) != 3:
        _fail(
            "третий элемент load_thread_state оказался КОЛИЧЕСТВОМ, а не НОМЕРОМ",
            f"после удаления среднего из трёх вернулось {after[2]!r}, ждали 3."
            "\n  Это номер последнего выданного seq: append выдаёт start_seq + 1."
            "\n  seq — ordering key хвостового читателя TUI, и на записи с"
            "\n  меньшим номером тот перестраивает живой трейс.",
        )


def check_message_id_guaranteed_on_read(store: Any) -> None:
    """У сообщения на чтении есть ``id``, даже если писали без него.

    Без ``id`` ключ идемпотентности уходит в ветку ``payload:<json>``,
    нестабильную к любому изменению словаря: добавили ключ — и то же самое
    сообщение считается новым.
    """
    store.append_messages(
        SYS, "t-noid", [{"role": "user", "content": "без id"}]
    )
    got = store.load_messages(SYS, "t-noid")
    if not got:
        _fail("сообщение без id не сохранилось", "ждали одно сообщение")
    mid = got[0].get("id")
    if not isinstance(mid, str) or not mid:
        _fail(
            "у прочитанного сообщения нет id",
            "реализация обязана проставить его на чтении (см. ensure_message_id):"
            "\n  иначе ключ идемпотентности уедет в ветку payload:<json>,"
            "\n  нестабильную к любому изменению формы сообщения.",
        )


def check_meta_roundtrip(store: Any) -> None:
    """Meta записалась и прочиталась, незнакомые секции целы."""
    store.save_meta(
        SYS, "t-meta", {"token_usage": {"total": 7}, "чужое": {"x": 1}}
    )
    meta = store.load_meta(SYS, "t-meta")
    if meta.get("token_usage", {}).get("total") != 7:
        _fail("meta не сохранилась", f"прочиталось {meta!r}")
    if meta.get("чужое") != {"x": 1}:
        _fail(
            "незнакомая секция meta потерялась",
            "форма meta ОТКРЫТАЯ: секцией tui владеет другой процесс, а сервер"
            "\n  отдаёт meta наружу дословно. Незнакомые ключи хранятся как есть.",
        )


def check_meta_is_stamped_on_write(store: Any) -> None:
    """Хранилище штампует meta при записи: версия схемы и метки времени.

    НЕСУЩЕЕ требование, и ловится оно только вторым прогоном: без
    ``state_schema_version`` загрузка треда падает ``StateSchemaError``, но
    ПЕРВЫЙ прогон проходит нормально — проверять просто нечего, пока в meta нет
    состояния агента. Наши реализации разошлись здесь пятыми, и обнаружилось
    это на сквозной приёмке SDK, а не в юнит-тестах.
    """
    store.save_meta(SYS, "t-stamp", {"shared": {"working_memory": "wm"}})
    meta = store.load_meta(SYS, "t-stamp")

    if not meta.get("state_schema_version"):
        _fail(
            "хранилище не проставило state_schema_version при записи",
            "без него следующий прогон ОТКАЖЕТСЯ читать тред:"
            "\n  validate_thread_meta_for_load поднимет StateSchemaError."
            "\n  Готовое значение даёт ThreadMetaMapper.ensure_v2_meta(meta).",
        )
    for field in ("created_at", "updated_at"):
        if not meta.get(field):
            _fail(
                f"хранилище не проставило {field} при записи",
                "created_at — момент рождения треда (не сбрасывается никогда,"
                "\n  в том числе компакцией), updated_at — момент этой записи.",
            )

    # created_at не переписывается повторной записью.
    born = meta["created_at"]
    store.save_meta(SYS, "t-stamp", {**meta, "x": 1})
    if store.load_meta(SYS, "t-stamp").get("created_at") != born:
        _fail(
            "повторная запись сбросила created_at",
            "это момент рождения треда, он не меняется никогда",
        )


def check_runtime_flag_is_pointwise(store: Any) -> None:
    """Флаг рантайма не затирает чужие секции, а снятие последнего убирает секцию."""
    store.save_meta(
        SYS, "t-flag", {"token_usage": {"total": 7}, "runtime": {}}
    )
    store.patch_runtime_flag(SYS, "t-flag", "summarizing", active=True)

    meta = store.load_meta(SYS, "t-flag")
    if meta.get("runtime", {}).get("summarizing") is not True:
        _fail("флаг не взвёлся", f"runtime={meta.get('runtime')!r}")
    if meta.get("token_usage", {}).get("total") != 7:
        _fail(
            "патч флага затёр чужие секции meta",
            "сюда пишут ВО ВРЕМЯ работы, параллельно с самим прогоном:"
            "\n  read-modify-write целого meta потеряет то, что записал прогон.",
        )

    store.patch_runtime_flag(SYS, "t-flag", "summarizing", active=False)
    meta = store.load_meta(SYS, "t-flag")
    if "runtime" in meta:
        _fail(
            "пустая секция runtime осталась в meta",
            "снятие последнего флага убирает секцию ЦЕЛИКОМ: «нет флагов» и"
            "\n  «пустой словарь» обязаны читаться одинаково, иначе переезд на"
            "\n  другое хранилище меняет форму meta — а её читают TUI и сервер.",
        )


def check_update_runtime_meta(store: Any) -> None:
    """Произвольные значения в ``runtime`` не трогают остальное."""
    store.save_meta(SYS, "t-rt", {"token_usage": {"total": 3}})
    store.update_runtime_meta(SYS, "t-rt", {"trace_ids": ["a", "b"]})
    meta = store.load_meta(SYS, "t-rt")
    if meta.get("runtime", {}).get("trace_ids") != ["a", "b"]:
        _fail(
            "update_runtime_meta не записал",
            f"runtime={meta.get('runtime')!r}",
        )
    if meta.get("token_usage", {}).get("total") != 3:
        _fail("update_runtime_meta затёр чужую секцию", f"meta={meta!r}")


def check_every_write_path_stamps_meta(store: Any) -> None:
    """Штампы meta ставит КАЖДЫЙ путь записи, а не только ``save_meta``.

    Набор проверял штампы только у ``save_meta``, и две наши собственные
    реализации разошлись ровно здесь, оставаясь зелёными: ``FileThreadStore``
    штампует и в ``patch_runtime_flag``/``update_runtime_meta``, а
    ``MemoryThreadStore`` писал только секцию ``runtime``.

    Расхождение дорогое: без ``state_schema_version`` следующий прогон
    ОТКАЖЕТСЯ читать тред, и заметно это лишь на ВТОРОМ прогоне — первый
    проходит нормально. А ``updated_at`` после патча флага — то, по чему
    хвостовой читатель понимает, что тред жив.
    """
    # ВАЖНО: тред заводится САМИМ проверяемым путём, без предварительного
    # ``save_meta``. Иначе штампы достались бы в наследство от него, и
    # проверка стала бы пустой — ровно так расхождение и пряталось.
    for label, write in (
        (
            "patch_runtime_flag",
            lambda tid: store.patch_runtime_flag(
                SYS, tid, "summarizing", active=True
            ),
        ),
        (
            "update_runtime_meta",
            lambda tid: store.update_runtime_meta(
                SYS, tid, {"trace_ids": ["z"]}
            ),
        ),
    ):
        tid = f"t-stamp-{label}"
        write(tid)
        meta = store.load_meta(SYS, tid)

        if not meta.get("state_schema_version"):
            _fail(
                f"{label} не проставил state_schema_version",
                "без версии схемы следующий прогон откажется читать тред —"
                "\n  и увидите вы это только на ВТОРОМ прогоне.",
            )
        for field in ("created_at", "updated_at"):
            if not meta.get(field):
                _fail(
                    f"{label} не проставил {field}",
                    "штампы ставит каждый путь записи, а не только save_meta.",
                )


def check_discard_updates_the_count(store: Any) -> None:
    """После ``discard_message`` номер последнего seq и счёт расходятся — законно.

    А вот СЧЁТ записей обязан уменьшиться: его читает TUI (список сессий,
    «N сообщ.»). ``MemoryThreadStore`` его не декрементил, и обе реализации
    проходили набор — расхождение вылезало у пользователя как «сообщений
    столько же, а в треде меньше».
    """
    tid = "t-discard-count"
    store.append_messages(
        SYS,
        tid,
        [
            {"role": "user", "content": "a", "id": "m1"},
            {"role": "user", "content": "b", "id": "m2"},
        ],
    )
    before = len(store.load_messages(SYS, tid))

    removed = store.discard_message(SYS, tid, "m2")

    if not removed:
        _fail("discard_message не удалил существующее сообщение", "id=m2")
    after = len(store.load_messages(SYS, tid))
    if after != before - 1:
        _fail(
            "после discard_message история не стала короче",
            f"было {before}, стало {after} — TUI покажет неверное число сообщений",
        )


def check_replace_messages_keeps_the_trace(store: Any) -> None:
    """Замещение истории не должно уносить трассу субагентов и meta.

    Компакция у хранилищ без ``archive_and_replace`` замещает историю. Пока
    это делалось парой ``reset`` + ``append_messages``, каждая компакция
    стирала ``subagent_trace`` (``reset`` по контракту сносит ВЕСЬ тред) и
    открывала окно, в котором треда не существовало вовсе.

    Способность необязательная: у кого её нет, проверка пропускается.
    """
    replace = getattr(store, "replace_messages", None)
    if not callable(replace):
        return

    tid = "t-replace"
    store.save_meta(SYS, tid, {"token_usage": {"total": 5}})
    store.append_messages(
        SYS, tid, [{"role": "user", "content": "старое", "id": "m1"}]
    )
    for n in range(2):
        store.append_subagent_trace(
            SYS,
            tid,
            parent_call_id=f"p{n}",
            child_run_id=f"c{n}",
            kind="call",
            payload={"x": n},
        )
    before = store.read_subagent_trace(SYS, tid)

    replace(SYS, tid, [{"role": "system", "content": "сводка", "id": "s1"}])

    left = store.load_messages(SYS, tid)
    if [m.get("id") for m in left] != ["s1"]:
        _fail("replace_messages не заместил историю", f"осталось {left!r}")
    after = store.read_subagent_trace(SYS, tid)
    if not after:
        _fail(
            "replace_messages потерял трассу субагентов",
            "замещение истории не повод стирать историю вызовов детей",
        )
    # Непустоты МАЛО. Первая редакция проверяла только её, и мимо прошло вот
    # что: трасса восстанавливалась через `append_subagent_trace`, а тот
    # безусловно ставит свежие `ts` и `seq`. После каждой компакции у всех
    # записей дата равнялась моменту компакции, нумерация начиналась заново —
    # трасса переставала быть хронологией, ради которой её и спасали.
    if len(after) != len(before):
        _fail(
            "replace_messages изменил ЧИСЛО записей трассы",
            f"было {len(before)}, стало {len(after)}",
        )
    for was, now in zip(before, after):
        for field in ("ts", "seq", "parent_call_id", "child_run_id", "kind"):
            if was.get(field) != now.get(field):
                _fail(
                    f"replace_messages переписал поле {field!r} у трассы",
                    f"было {was.get(field)!r}, стало {now.get(field)!r}; "
                    "восстанавливать трассу надо записями как есть, а не "
                    "повторным append — тот проставляет штампы заново",
                )
    if not store.load_meta(SYS, tid).get("token_usage"):
        _fail(
            "replace_messages потерял meta",
            "секции meta не относятся к истории",
        )


def check_discard(store: Any) -> None:
    """Удаление по id, ``False`` на несуществующем."""
    store.append_messages(SYS, "t-disc", [_msg("d1"), _msg("d2")])
    if store.discard_message(SYS, "t-disc", "d1") is not True:
        _fail("discard_message не вернул True на существующем сообщении", "")
    left = [m["id"] for m in store.load_messages(SYS, "t-disc")]
    if left != ["d2"]:
        _fail("discard убрал не то сообщение", f"осталось {left!r}")
    if store.discard_message(SYS, "t-disc", "нет-такого") is not False:
        _fail("discard_message не вернул False на отсутствующем сообщении", "")


def check_reset(store: Any) -> None:
    """Сброс чистит и историю, и meta. Возврат — свободный текст."""
    store.append_messages(SYS, "t-reset", [_msg("r1")])
    store.save_meta(SYS, "t-reset", {"x": 1})
    out = store.reset(SYS, "t-reset")
    if not isinstance(out, list):
        _fail("reset вернул не список", f"вернул {type(out).__name__}")
    if store.load_messages(SYS, "t-reset") != []:
        _fail("reset не очистил историю", "")
    if store.load_meta(SYS, "t-reset") != {}:
        _fail("reset не очистил meta", "")


def check_subagent_trace(store: Any) -> None:
    """Запись трассы содержит все шесть полей, включая ``ts`` и ``seq``.

    Наши реализации разошлись здесь: файловая писала шесть полей, in-memory
    четыре, а проверка смотрела только на ``kind`` и оставалась зелёной.
    """
    rec = store.append_subagent_trace(
        SYS,
        "t-trace",
        parent_call_id="call-1",
        child_run_id="run-1",
        kind="call",
        payload={"name": "fs_read_file"},
    )
    if not isinstance(rec, dict):
        _fail(
            "append_subagent_trace вернул не запись",
            f"вернул {type(rec).__name__}. Возвращается САМА записанная запись"
            "\n  с проставленными хранилищем ts и seq; None не возвращается никогда.",
        )
    missing = TRACE_FIELDS - set(rec)
    if missing:
        _fail(
            f"в записи трассы нет полей: {', '.join(sorted(missing))}",
            f"форма замкнута, обязательны все шесть: {', '.join(sorted(TRACE_FIELDS))}."
            "\n  ts и seq проставляет ХРАНИЛИЩЕ, а не вызывающий.",
        )
    if not isinstance(rec.get("seq"), int):
        _fail("seq трассы не целое", f"seq={rec.get('seq')!r}")

    store.append_subagent_trace(
        SYS,
        "t-trace",
        parent_call_id="call-2",
        child_run_id="run-2",
        kind="finished",
        payload={},
    )
    trace = store.read_subagent_trace(SYS, "t-trace")
    if [r.get("kind") for r in trace] != ["call", "finished"]:
        _fail("трасса вернулась не в порядке записи", f"вернулось {trace!r}")
    if [r.get("seq") for r in trace] != [1, 2]:
        _fail(
            "seq трассы не монотонен с единицы",
            f"вернулось {[r.get('seq') for r in trace]!r}, ждали [1, 2]",
        )


#: Поведенческие проверки. Каждой — свежее хранилище: общее состояние между
#: проверками пряталo бы поломки за порядком запуска.
BEHAVIOUR_CHECKS: tuple = (
    check_missing_thread_is_empty,
    check_roundtrip,
    check_idempotent,
    check_known_keys_mutated_in_place,
    check_seq_is_a_number_not_a_count,
    check_message_id_guaranteed_on_read,
    check_meta_roundtrip,
    check_meta_is_stamped_on_write,
    check_runtime_flag_is_pointwise,
    check_every_write_path_stamps_meta,
    check_discard_updates_the_count,
    check_replace_messages_keeps_the_trace,
    check_update_runtime_meta,
    check_discard,
    check_reset,
    check_subagent_trace,
)

STRUCTURE_CHECKS: tuple = (
    check_methods_present,
    check_methods_are_sync,
    check_signatures,
)


def assert_conforms(store_factory: Callable[[], Any]) -> List[str]:
    """Проверить реализацию ``ThreadStore`` целиком.

    ``store_factory`` вызывается заново перед каждой поведенческой проверкой —
    состояние между ними не переезжает.

    Поднимает :class:`ThreadStoreContractError` с объяснением на первом
    расхождении. Возвращает список пройденных проверок — тесту удобно
    утверждать, что их столько, сколько ожидалось.
    """
    return run_checks(store_factory, STRUCTURE_CHECKS, BEHAVIOUR_CHECKS)


def run_checks(
    store_factory: Callable[[], Any], structure: tuple, behaviour: tuple
) -> List[str]:
    """Прогон набора проверок: структурные — на одном экземпляре, каждая
    поведенческая — на свежем (с ``ensure_ready``, если он есть). Общий для
    обычного и durable-контракта."""
    passed: List[str] = []
    store = store_factory()
    for check in structure:
        check(store)
        passed.append(check.__name__)
    for check in behaviour:
        fresh = store_factory()
        if hasattr(fresh, "ensure_ready"):
            fresh.ensure_ready()
        check(fresh)
        passed.append(check.__name__)
    return passed


# --- асинхронные реализации ------------------------------------------------


class _AwaitingFacade:
    """Синхронный вид асинхронного хранилища — чтобы проверки были ТЕ ЖЕ.

    Двенадцать поведенческих свойств не зависят от того, корутинный метод или
    нет: идемпотентность, мутация ``known_keys``, ``max_seq`` как номер, шесть
    полей трассы. Дублировать их асинхронными копиями значило бы завести второй
    набор, который разойдётся с первым — ровно та беда, из-за которой этот
    модуль и появился.

    Корутины исполняются на ОДНОМ loop, а не через ``asyncio.run`` на каждый
    вызов: клиент вроде ``redis.asyncio`` держит соединение между вызовами, и
    новый loop на каждое обращение убил бы его вторым же (проверено:
    ``RuntimeError: Event loop is closed``).
    """

    def __init__(self, store: Any, loop: Any) -> None:
        self._store = store
        self._loop = loop

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._store, f"a{name}")

        def run(*args: Any, **kwargs: Any) -> Any:
            return self._loop.run_until_complete(target(*args, **kwargs))

        return run


def check_async_methods_present(store: Any) -> None:
    """Все одиннадцать ``a``-методов есть и корутинны."""
    from gc_core.thread_store_async import ASYNC_CONTRACT_METHODS

    missing = [
        m
        for m in ASYNC_CONTRACT_METHODS
        if not inspect.iscoroutinefunction(getattr(store, m, None))
    ]
    if not missing:
        return
    present_sync = [m for m in missing if callable(getattr(store, m, None))]
    hint = (
        f"\n  Есть, но не корутинны: {', '.join(present_sync)}."
        "\n  Половинчатая реализация опаснее полного отсутствия: часть записей"
        "\n  уйдёт в базу, часть потеряется молча."
        if present_sync
        else ""
    )
    _fail(
        f"{type(store).__name__}: нет корутинных методов: {', '.join(missing)}",
        f"AsyncThreadStore ждёт все {len(ASYNC_CONTRACT_METHODS)}: {', '.join(ASYNC_CONTRACT_METHODS)}{hint}",
    )


def check_accepted_by_the_port(store: Any) -> None:
    """Фабрика ``athread_store`` принимает реализацию и не заворачивает в адаптер."""
    from gc_core.thread_store_async import athread_store

    port = athread_store(store)
    if port is not store:
        _fail(
            f"{type(store).__name__}: порт завернул реализацию в адаптер",
            f"вместо неё используется {type(port).__name__} — значит ядро сочло "
            "хранилище\n  синхронным, и нативного ожидания не будет.",
        )


ASYNC_STRUCTURE_CHECKS: tuple = (
    check_async_methods_present,
    check_accepted_by_the_port,
)


def assert_conforms_async(store_factory: Callable[[], Any]) -> List[str]:
    """Проверить реализацию ``AsyncThreadStore`` — тем же набором свойств.

    Синхронная функция: её зовут из обычного теста, loop создаётся внутри::

        def test_my_redis_store():
            assert_conforms_async(lambda: RedisThreadStore(url=TEST_URL))
    """
    import asyncio

    passed: List[str] = []
    store = store_factory()
    for check in ASYNC_STRUCTURE_CHECKS:
        check(store)
        passed.append(check.__name__)

    loop = asyncio.new_event_loop()
    try:
        for check in BEHAVIOUR_CHECKS:
            check(_AwaitingFacade(store_factory(), loop))
            passed.append(check.__name__)
    finally:
        loop.close()
    return passed


__all__ = [
    "ASYNC_STRUCTURE_CHECKS",
    "BEHAVIOUR_CHECKS",
    "CONTRACT_METHODS",
    "STRUCTURE_CHECKS",
    "TRACE_FIELDS",
    "ThreadStoreContractError",
    "assert_conforms",
    "assert_conforms_async",
]

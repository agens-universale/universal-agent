# Треды формата develop — эталон для хранилищ v2

Файлы в `root/` создал код develop `1781889661d54620060487010fcfc37445f9ff29`
(14.10.2026), до любых изменений хранения. Это эталон: на всех этапах
переезда (спека `docs/superpowers/specs/2026-10-14-agent-storage-v2-design.md`)
эти треды обязаны открываться, продолжаться и показываться фронтендами.

**Не перегенерировать.** Перегенерация кодом после этапа 1 спрятала бы любую
регрессию формата. Новые случаи — отдельными каталогами со своим README.

## Как получены

Одноразовым генератором (в репозиторий не входит): `parity_harness` —
`ScriptedBackend`, `install`, `parity_config` — гонит настоящие
`acreate_run_context` и `AgentExecutor.arun`; модель заменена скриптом.
Фронтендовые индексы записаны настоящим кодом фронтендов.

| Ключ манифеста | system / thread | Как получен |
|---|---|---|
| `cli_two_turns` | `cli` / `legacy-cli` | 2 хода: `exec_calculator` → `turn_finish`; `fs_write_file` → `turn_finish` |
| `cli_compacted` | `cli` / `legacy-compacted` | ход, затем `acompact_thread` (сводка XML из шести секций + патч памяти) |
| `cli_subagents` | `cli` / `legacy-subagents` | ход, затем `append_subagent_trace_record` для `call`, `user`, `intermediate_reply` |
| `tui_session` | `tui` / `legacy-tui` | 2 хода с `is_tui=True` (журнал правок ведётся только в TUI) и `FileTaskStore.update_tui_meta` (`title`, `status`) |
| `acp_session` | `acp` / `0b1c2d3e-…-000000000001` | ход, затем `SessionIndex.create` |
| `chat_dialog` | `responses` / `legacy-chat` | ход, затем `ResponseIndex.save` |
| `sdk_image` | `sdk` / `legacy-image` | `AsyncAgent.run(..., attachments=[[png 8×8]])`; проба зрения с фиксированным кодом (`probe.make_challenge` подменён) |

Отклонения от черновика плана:

- журнал правок есть только у `tui_session`: по умолчанию `agent_edits.record`
  включён лишь для TUI, и тред CLI журнала не получает — как в жизни;
- ручная компакция на develop делает два запроса (сводка и патч памяти),
  сводка обязана быть XML из шести секций (`summary_spec.SUMMARY_SECTIONS`);
- в `root/workspace/` — файлы, которые записали ходы. Снимков
  `.gc/agent_edits/` нет: правка TUI создавала новый файл.

## Пути

Абсолютные пути заменены плейсхолдерами `<THREADS_DIR>`, `<WORKSPACE>`,
`<ROOT>`. `gc_testkit.legacy_threads.install_legacy_threads(root)` кладёт
копию в `root` и подставляет настоящие пути. Служебные `*.lock`, `locks/` и
`images/_staging/` не заморожены.

`manifest.json` считается по замороженным файлам, а не пишется руками:
`messages` — строки `data_<s>_<t>.jsonl`, `archives` — файлы
`__pre_summary__`, `images` — файлы `images/<s>_<t>_*`, `subagent_events` и
`edits` — строки соответствующих журналов, `title` — `meta.tui.title`,
`first_user_text` — `content` первой записи с `role == "user"`.

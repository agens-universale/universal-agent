# Default Coworker Pack

Skill pack для широких нетривиальных задач «цифрового коллеги»: исследование,
личная помощь, фоновые бизнес-процессы, оформление результатов и границы автономности.

Спека: [SPEC.md](SPEC.md). Принципы проекта: [docs_md/principles.md](../../docs_md/principles.md).

## Когда использовать

- Пользователь делегирует результат, а не задаёт один вопрос.
- Нужны исследование, координация, личная организация или ongoing ops.
- Задача нетривиальна, но не попадает сразу в узкий specialized pack.

**Когда НЕ использовать:**
- Простые прямые ответы и короткие объяснения.
- One-step trivial команды.
- Задача однозначно покрыта specialized pack (coding → supercoder, и т.д.).

## Вызов

```
gc-cli -q "use skill default-coworker:using-default-coworker"
```

Или по намерению: агент видит bootstrap в `skills_list`, классифицирует задачу,
грузит один primary skill.

## Скиллы

| Canonical ID | Назначение |
|---|---|
| `default-coworker:using-default-coworker` | **Bootstrap.** Единственный router пакета. |
| `default-coworker:research` | Исследование и аналитика с evidence discipline. |
| `default-coworker:personal-assistant` | Личная организация, черновики, приоритизация. |
| `default-coworker:background-business` | Ongoing monitoring и бизнес-процессы. |
| `default-coworker:deliverables-evidence` | Формат результата и уровень верификации. |
| `default-coworker:handoff-autonomy` | Autonomy ladder, approvals, handoff. |

Inner-скилы **не ссылаются друг на друга** — только bootstrap маршрутизирует.
Внешние packs (`supercoder`, `topic-research`, …) — тоже только из bootstrap.

## Список internal

```
skills_list(pack="default-coworker")
```

## Override

Переопределить скилл пакета для проекта:

```
<repo>/.gc/skills/default-coworker/<skill-id>/SKILL.md
```

Переопределить манифест:

```
<repo>/.gc/skills/default-coworker/pack.yaml
```

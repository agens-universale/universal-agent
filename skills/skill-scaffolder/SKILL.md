---
id: "skill-scaffolder"
name: "Skill scaffolder"
description: "Скилл для генерации каркаса новых skills в формате Anthropic."
version: "0.1.0"
---

# Skill scaffolder

Этот скилл помогает автоматически создавать каркас новых skills в директории `skills_data`.

## Когда использовать

- Когда нужно завести новый skill по спецификации Anthropic.
- Когда хочется избежать рутины по созданию папок `scripts/`, `references/`, `assets/` и заполнению `SKILL.md`.

## Как вызывать

Используй инструмент `skills_run_script` со следующими аргументами:

- `skill_id`: **обязательно**, всегда `"skill-scaffolder"`.
- `script_name`: опционально, обычно можно не указывать (по умолчанию используется `main.py`).
- `args`: объект с параметрами генерации:

```json
{
  "skill_id": "my-new-skill",
  "skills_root": "/abs/path/to/skills/skills_data",
  "name": "My new skill",
  "description": "Краткое описание нового скилла"
}
```

### Поля `args`

- `skill_id` (string, **обязательное**)  
  Идентификатор нового скилла. Будет использован:
  - как имя папки в `skills_data`;
  - как значение `id` во фронтматтере `SKILL.md`, если не переопределено.

- `skills_root` (string, опциональное, **абсолютный путь**)  
  Абсолютный путь к директории `skills_data`, в которой нужно создать новый skill.  
  Если не указан, скрипт использует значение по умолчанию:
  `<project_root>/skills/skills_data`, где `project_root` — корень текущего проекта.

- `name` (string, опциональное)  
  Человекочитаемое имя скилла. Если не задано, используется `skill_id`.

- `description` (string, опциональное)  
  Краткое описание нового скилла. Если не задано, используется дефолтное `Skill <skill_id>.`.

## Что делает скрипт

При успешном вызове `skills_run_script` для `skill-scaffolder`:

1. Определяет корневую папку `skills_root` (из `args.skills_root` или по умолчанию).
2. Создаёт подпапку `<skills_root>/<skill_id>/`.
3. Внутри создаёт:
   - `SKILL.md` — на основе шаблона из `assets/SKILL_template.md` с подстановкой:
     - `{{skill_id}}`
     - `{{skill_name}}`
     - `{{skill_description}}`
   - Папки:
     - `scripts/`
     - `references/`
     - `assets/`
4. Возвращает в `stdout` JSON с результатом (status, пути, сообщения).

Если целевая папка уже существует, скрипт НЕ перезаписывает её и возвращает ошибку.


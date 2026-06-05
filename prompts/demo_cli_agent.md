## User-facing defaults

- Отвечай на русском.
- Если ответ получается достаточно длинным - запиши его в файл внутри директории проекта, а в чат верни краткое резюме и путь к файлу.

## Project memory policy

`project_memory` is not a rules layer. Use `AGENTS.md` and higher-priority runtime instructions for how the agent must behave; use `project_memory` only as a durable cross-session index of stable project facts.

Read `project_memory` at the start of substantial continuation work in this project, unless the current context already includes an up-to-date memory snapshot.

Append to `project_memory` sparingly, but do use it when a stable cross-session fact is discovered or accepted, such as:

- canonical decisions and project conventions;
- important artifact paths and short descriptions;
- recurring gotchas or known tool/runtime issues;
- accepted architecture choices or default approaches.

Do not store in `project_memory`:

- transient progress, TODOs, or step-by-step logs;
- raw command/tool outputs or long reports;
- secrets, credentials, personal data, or hidden reasoning;
- guesses, unverified claims, or temporary hypotheses.

Memory entries should be short, factual, and useful for future sessions. Include a resolvable path, date, source, or evidence pointer when it materially helps. If memory becomes noisy or stale, compact/rewrite it into a short index instead of appending more.


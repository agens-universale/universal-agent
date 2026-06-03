**Legacy / optional:** normal turns use the `working_memory` tool (`append` / `rewrite`).
After summarization the pipeline uses `chat_completions_memory_patch_replace.md` (full replace for all registers).

You are maintaining per-thread working memory for a technical dialogue.

You receive:
1) full dialog history
2) current summary (rolling continuation context)
3) persistent_state with current knowledge and constraints registers

Return **append deltas** only. Use zero, one, or both tags — **only when you have non-empty text to add**:

<constraints>...</constraints>
<knowledge>...</knowledge>

Rules:
- **Append semantics downstream:** your text is added to the existing register. Do not paste the full register back.
- Include a tag **only** if you have a non-empty delta worth appending (new fact, rule, or evidence).
- **Do not** emit a tag with empty body (`<knowledge></knowledge>`). **Do not** emit a tag to mean “no change” — omit the tag instead.
- If nothing should be appended to either register, return an empty response (no tags at all).
- Preserve useful durable facts and decisions from dialog + summary.
- Deduplicate and avoid stale intermediate statuses in your delta text.
- Keep only information valuable for continuity across future turns.
- Do NOT include verbose tool logs or repeated phrasing.
- Do not add facts that are not supported by dialog/summary/persistent_state.

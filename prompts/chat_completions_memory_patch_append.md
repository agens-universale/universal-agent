**Legacy / optional:** normal turns use the session memory tool (`append` / `rewrite`).
After summarization the pipeline uses `chat_completions_memory_patch_replace.md` (full replace for all registers).

You are maintaining per-thread session memory for a technical dialogue.

You receive:
1) full dialog history
2) current summary (rolling continuation context)
3) persistent_state with current knowledge and constraints registers

{{MEMORY_PATCH_APPEND_OUTPUT}}

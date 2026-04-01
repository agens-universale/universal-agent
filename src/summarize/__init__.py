"""Пакет суммаризации диалога.

Подмодули:
- ``compaction`` — порог символов, вызов оркестратора, ``apply_summary_state_patch``;
- ``thread_summarizer`` — сценарий батчей LLM и сборка результата;
- ``dialog_history`` — ходы, батчи, текст истории для промпта;
- ``summary_validation`` — эвристики пригодности сводки;
- ``summary_serialization`` — текст сообщений для промпта/метрик, признак dialog summary в истории;
- ``summary_message_body`` — тело сообщения-резюме;
- ``summary_llm_steps`` — сжатие и починка сводки (отдельные вызовы LLM);
- ``memory_suggestions`` — шаг предложений для долговременной памяти;
- ``llm_chat`` — chat completions и логирование;
- ``prompt_loading`` — загрузка шаблона промпта;
- ``config`` — параметры порогов (в корне пакета рядом).
"""

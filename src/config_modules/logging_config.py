from pydantic import BaseModel, ConfigDict


class LoggingConfig(BaseModel):
    """Параметры логирования рантайма (не путать со stdlib logging)."""

    model_config = ConfigDict(extra="forbid")

    full_log_backend: bool = False
    console_trace: bool = True
    dump_storage: bool = False
    save_logs: bool = True

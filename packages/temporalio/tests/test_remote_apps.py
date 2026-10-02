"""Preview storage failure never fails the operation."""


def test_preview_storage_failure_does_not_fail_the_operation(
    tmp_path, monkeypatch
):
    import sqlite3

    from gc_ua_temporalio.preview import PreviewSink, commit_preview
    from gc_ua_temporalio.storage.technical import TechnicalStore
    from gc_ua_temporalio.storage.views import ViewEvents

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    store = TechnicalStore(tmp_path / "preview.sqlite")
    monkeypatch.setattr(ViewEvents, "emit", unavailable)
    sink = PreviewSink(store, "owner", "operation", attempt=1)
    sink.delta("mcp_progress", "half complete")
    commit_preview(store, "owner", "operation")

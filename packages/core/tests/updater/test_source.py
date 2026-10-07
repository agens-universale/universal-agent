from gc_core.app_version import InstallSource
from gc_core.updater.source import resolved_source


def test_resolved_source_from_install():
    src = InstallSource(
        url="https://gitverse.ru/x/y",
        ref="master",
        commit="abc123",
        editable=False,
    )
    r = resolved_source(_source=lambda: src)
    assert (
        r.url == "https://gitverse.ru/x/y"
        and r.ref == "master"
        and r.commit == "abc123"
    )


def test_resolved_source_editable_is_none():
    src = InstallSource(
        url="file:///repo", ref=None, commit=None, editable=True
    )
    assert resolved_source(_source=lambda: src) is None


def test_resolved_source_no_direct_url_is_none():
    assert resolved_source(_source=lambda: None) is None

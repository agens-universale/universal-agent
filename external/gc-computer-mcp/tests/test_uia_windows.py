import sys

import pytest


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_provider_returns_desktop_with_children():
    from gc_computer_mcp.core.uia_windows import WindowsUIAProvider

    provider = WindowsUIAProvider()
    root = provider.desktop_root()
    assert root is not None
    children = list(provider.iter_children(root))
    assert len(children) >= 1  # at least one window exists (taskbar etc.)


def test_children_with_retry_succeeds_after_transient_failure():
    """_children_with_retry retries when GetChildren raises a transient error."""
    from gc_computer_mcp.core.uia_windows import _children_with_retry

    class FlakyControl:
        def __init__(self):
            self.calls = 0

        def GetChildren(self):
            self.calls += 1
            if self.calls < 2:
                raise RuntimeError("simulated COMError")
            return ["child-a", "child-b"]

    ctrl = FlakyControl()
    result = _children_with_retry(ctrl)
    assert result == ["child-a", "child-b"]
    assert ctrl.calls == 2  # first call failed, second succeeded


def test_children_with_retry_gives_up_after_max_retries():
    from gc_computer_mcp.core.uia_windows import _children_with_retry

    class AlwaysFails:
        def __init__(self):
            self.calls = 0

        def GetChildren(self):
            self.calls += 1
            raise RuntimeError("always fails")

    ctrl = AlwaysFails()
    import pytest
    with pytest.raises(RuntimeError):
        _children_with_retry(ctrl)
    assert ctrl.calls >= 3  # initial + retries


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_windows_provider_node_from_hwnd_desktop():
    """GetDesktopWindow HWND should resolve to a non-None UIA node via node_from_hwnd."""
    import ctypes

    from gc_computer_mcp.core.uia_windows import WindowsUIAProvider

    desktop_hwnd = ctypes.windll.user32.GetDesktopWindow()
    provider = WindowsUIAProvider()
    node = provider.node_from_hwnd(desktop_hwnd)
    assert node is not None
    # Must expose the minimal wrapped interface — bounds, name, etc.
    _ = node.bounds


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_windows_provider_node_from_hwnd_invalid():
    from gc_computer_mcp.core.uia_windows import WindowsUIAProvider

    provider = WindowsUIAProvider()
    # HWND 0 is defined as invalid per Win32 docs
    assert provider.node_from_hwnd(0) is None


class _FakeValuePattern:
    def __init__(self, value: str):
        self.Value = value


class _FakeCtrl:
    def __init__(self, control_type_name: str, value_pattern=None,
                 raises_on_get_pattern: bool = False):
        self.ControlTypeName = control_type_name
        self._vp = value_pattern
        self._raises = raises_on_get_pattern

    def GetValuePattern(self):
        if self._raises:
            raise RuntimeError("pattern unsupported (simulated COMError)")
        return self._vp


def test_wrapped_value_empty_for_non_input_control():
    """_Wrapped.value returns '' for Button/Pane/Window — skips the COM call."""
    from gc_computer_mcp.core.uia_windows import _Wrapped
    w = _Wrapped(_FakeCtrl("ButtonControl"))
    assert w.value == ""


def test_wrapped_value_reads_value_pattern_for_edit_control():
    """For EditControl, _Wrapped.value returns ValuePattern.Value."""
    from gc_computer_mcp.core.uia_windows import _Wrapped
    ctrl = _FakeCtrl("EditControl", value_pattern=_FakeValuePattern("Дмитрий"))
    w = _Wrapped(ctrl)
    assert w.value == "Дмитрий"


def test_wrapped_value_reads_value_pattern_for_combobox_control():
    from gc_computer_mcp.core.uia_windows import _Wrapped
    ctrl = _FakeCtrl("ComboBoxControl", value_pattern=_FakeValuePattern("ru-RU"))
    w = _Wrapped(ctrl)
    assert w.value == "ru-RU"


def test_wrapped_value_empty_when_pattern_is_none():
    """Control that does not support ValuePattern must yield '' without raising."""
    from gc_computer_mcp.core.uia_windows import _Wrapped
    w = _Wrapped(_FakeCtrl("EditControl", value_pattern=None))
    assert w.value == ""


def test_wrapped_value_empty_when_get_pattern_raises():
    """Transient COMError from GetValuePattern must be swallowed → ''."""
    from gc_computer_mcp.core.uia_windows import _Wrapped
    w = _Wrapped(_FakeCtrl("EditControl", raises_on_get_pattern=True))
    assert w.value == ""

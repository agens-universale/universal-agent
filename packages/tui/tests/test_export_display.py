"""TXT-экспорт использует display_content; JSON остаётся wire-faithful."""

from gc_tui.services.export import export_thread


def _seg(payloads):
    return [
        {
            "index": 0,
            "messages": [{"payload": p, "ts": "t"} for p in payloads],
            "compacted_by": None,
        }
    ]


def test_txt_uses_display_content():
    seg = _seg(
        [
            {
                "role": "user",
                "content": "<skill>\nдавай обсудим",
                "display_content": "давай обсудим",
            }
        ]
    )
    out = export_thread(
        seg, fmt="txt", api_mode="", thread_id="t", system="cli"
    )
    assert "давай обсудим" in out
    assert "<skill>" not in out  # портянка не попала


def test_txt_skips_empty_display():
    seg = _seg([{"role": "user", "content": "<skill>", "display_content": ""}])
    out = export_thread(
        seg, fmt="txt", api_mode="", thread_id="t", system="cli"
    )
    assert "<skill>" not in out  # mid-run доставка не выводится как USER


def test_json_keeps_wire():
    seg = _seg(
        [
            {
                "role": "user",
                "content": "<skill>\nдавай",
                "display_content": "давай",
            }
        ]
    )
    out = export_thread(
        seg, fmt="json", api_mode="", thread_id="t", system="cli"
    )
    assert "<skill>" in out  # JSON faithful: content=wire присутствует
    assert "давай" in out  # display_content рядом метаданными

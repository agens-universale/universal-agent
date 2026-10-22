"""Режим по умолчанию, который в канале без человека не работает."""

import pytest
from gc_testkit.settings import make_settings

from gc_core.message_policy.mode_messages import headless_mode_warning_text
from gc_core.security.permission_mode import headless_mode_warning


@pytest.mark.parametrize(
    "mode, expected",
    [("plan", "plan"), ("manual", "manual"), ("edit", None), ("auto", None)],
)
def test_only_modes_that_need_a_human_are_reported(mode, expected):
    settings = make_settings({"security.permissions.default_mode": mode})
    assert headless_mode_warning(settings) == expected
    assert headless_mode_warning(settings.settings) == expected


def test_default_config_is_not_reported():
    assert headless_mode_warning(make_settings()) is None


def test_text_names_the_mode_the_consequence_and_the_way_back():
    plan = headless_mode_warning_text("plan", "флаг --x edit")
    assert plan.startswith("режим разрешений по умолчанию — plan")
    assert "одобрить" in plan
    assert plan.endswith("Вернуть edit: флаг --x edit")
    assert "отказ" in headless_mode_warning_text("manual", "профиль")

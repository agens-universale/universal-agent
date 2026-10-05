"""Флаг `backend.vision` в схеме конфига: общая секция — `settings.backend.vision`,
значение модели — в её `backend_overrides` (так их читает «кто смотрит»,
`gc_core.vision.audience`)."""

from gc_core.config_modules.root import AppConfig
from gc_core.model_router import serialize_model_entry
from gc_core.vision.audience import _declared_blind
from gc_testkit.settings import make_settings


def test_the_general_flag_is_in_settings():
    assert AppConfig(backend={"vision": False}).backend.vision is False


def test_the_flat_name_lands_on_the_setting_path():
    assert (
        make_settings({"backend_vision": False}).settings.backend.vision
        is False
    )


def test_the_flag_is_unset_by_default():
    assert AppConfig().backend.vision is None


def test_a_model_flag_lands_in_its_backend_overrides():
    app_cfg = AppConfig(
        models={
            "gc": {
                "model": "gc_v1.6",
                "url": "http://proxy/v1/chat/completions",
                "token": "",
                "backend": {"vision": False},
            },
        },
        slots={"default": "gc"},
    )

    assert serialize_model_entry(app_cfg.models["gc"])[
        "backend_overrides"
    ] == {"vision": False}


def test_declared_blind_reads_the_general_flag():
    from gc_core.model_router import ResolvedModel

    resolved = ResolvedModel(
        alias=None,
        model="m",
        url="http://x/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="legacy",
    )
    assert _declared_blind(resolved, make_settings({"backend_vision": False}))
    assert not _declared_blind(resolved, make_settings())

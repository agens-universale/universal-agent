"""ImagesConfig: дефолты, валидатор «свежее сообщение живо целиком», проекция и читатель."""

import pytest
from gc_core.config_modules.images import ImagesConfig, read_images_config
from gc_core.config_modules.root import AppConfig
from gc_testkit.settings import make_settings
from pydantic import ValidationError


def test_defaults():
    cfg = ImagesConfig()
    assert (
        cfg.max_per_message,
        cfg.live_user_images,
        cfg.live_tool_images,
    ) == (10, 10, 3)


def test_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        ImagesConfig(max_images=5)


def test_live_user_images_must_cover_one_full_message():
    with pytest.raises(
        ValidationError,
        match="live_user_images must be >= images.max_per_message",
    ):
        ImagesConfig(max_per_message=10, live_user_images=9)
    assert (
        ImagesConfig(max_per_message=4, live_user_images=4).live_user_images
        == 4
    )


@pytest.mark.parametrize(
    "values", [{"max_per_message": 0}, {"live_tool_images": -1}]
)
def test_out_of_range_values_are_rejected(values):
    with pytest.raises(ValidationError):
        ImagesConfig(**values)


def test_tool_images_cannot_be_turned_off():
    """При 0 тул прикреплял бы снимок, который модель никогда не видит, — и звал бы
    его снова и снова. Не показывать картинки модели — снять тул, а не обнулять окно."""
    with pytest.raises(ValidationError, match="live_tool_images"):
        ImagesConfig(live_tool_images=0)
    assert ImagesConfig(live_tool_images=1).live_tool_images == 1


def test_app_config_carries_the_section():
    assert AppConfig().images == ImagesConfig()
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"images": {"max_per_message": 12, "live_user_images": 10}}
        )


def test_load_settings_exposes_images(tmp_path):
    """Значение из config.yml доходит до настроек и до читателя."""
    from gc_core.config import load_settings

    conf = tmp_path / "config.yml"
    conf.write_text(
        "images:\n"
        "  max_per_message: 4\n"
        "  live_user_images: 6\n"
        "  live_tool_images: 1\n",
        encoding="utf-8",
    )
    loaded = load_settings(str(conf))
    expected = ImagesConfig(
        max_per_message=4, live_user_images=6, live_tool_images=1
    )
    assert loaded.settings.images == expected
    assert read_images_config(loaded) == expected


def test_reader_without_the_section_returns_defaults():
    """Читатель зовётся на каждом запросе: без настроек — умолчания схемы."""
    assert read_images_config(None) == ImagesConfig()
    assert read_images_config(make_settings()) == ImagesConfig()
    assert read_images_config(AppConfig()) == ImagesConfig()


@pytest.mark.parametrize(
    "config",
    [
        {"images": {"max_per_message": 12, "live_user_images": 10}},
        {"images": {"unknown": 1}},
        {"images": "broken"},
    ],
)
def test_invalid_section_is_rejected_by_the_schema(config):
    """Невалидная секция до читателя не доходит: настройки отвергают её при
    сборке (на develop читатель молча откатывался к умолчаниям)."""
    with pytest.raises(ValidationError):
        make_settings(config)

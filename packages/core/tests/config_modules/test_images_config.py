"""ImagesConfig: дефолты, валидатор «свежее сообщение живо целиком», проекция и читатель."""

import pytest
from pydantic import ValidationError

from gc_core.config_modules.images import ImagesConfig, read_images_config
from gc_core.config_modules.root import AppConfig


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


def test_flatten_exposes_images(tmp_path):
    """Значение из config.yml доходит до config_dict['images'] и до читателя."""
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "images:\n"
        "  max_per_message: 4\n"
        "  live_user_images: 6\n"
        "  live_tool_images: 1\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load_config(str(conf))
    assert cfg["images"] == {
        "max_per_message": 4,
        "live_user_images": 6,
        "live_tool_images": 1,
    }
    assert read_images_config(cfg) == ImagesConfig(
        max_per_message=4, live_user_images=6, live_tool_images=1
    )


def test_reader_without_the_section_returns_defaults():
    """Читатель зовётся на каждом запросе, а у двойников ctx секции нет вовсе."""
    assert read_images_config({}) == ImagesConfig()
    assert read_images_config({"images": None}) == ImagesConfig()
    assert read_images_config(None) == ImagesConfig()


@pytest.mark.parametrize(
    "config_dict",
    [
        {"images": {"max_per_message": 12, "live_user_images": 10}},
        {"images": {"unknown": 1}},
        {"images": "broken"},
    ],
)
def test_reader_falls_back_to_defaults_on_an_invalid_section(config_dict):
    assert read_images_config(config_dict) == ImagesConfig()

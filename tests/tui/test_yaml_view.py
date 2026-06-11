from tui.services.yaml_view import (
    safe_yaml_dump,
    yaml_syntax_widget,
)


def test_safe_yaml_dump_tolerates_non_serializable_objects():
    # Реальный app._config содержит pydantic-объект под chat_completions_llm,
    # на котором yaml.safe_dump бросает RepresenterError. safe_yaml_dump не падает.
    from config_modules.chat_completions import ChatCompletionsLlmConfig
    cfg = {
        "a": 1,
        "flag": True,
        "nested": {"obj": ChatCompletionsLlmConfig(max_tokens=8192, completion_kwargs={})},
    }
    out = safe_yaml_dump(cfg)          # не должно бросать
    assert "a: 1" in out
    assert "8192" in out               # объект отрендерен строкой, не уронил дамп


def test_safe_yaml_dump_keeps_plain_types():
    out = safe_yaml_dump({"x": [1, 2], "y": {"z": True}})
    assert "x:" in out and "- 1" in out and "z: true" in out


def test_widget_wraps_yaml_in_syntax():
    from rich.syntax import Syntax
    w = yaml_syntax_widget("a: 1\nb:\n  - x\n")
    assert w.content.__class__ is Syntax        # Static(Syntax(...))
    assert "yaml" in w.content.lexer.aliases    # lexer is the YAML lexer

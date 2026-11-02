import pytest
from gc_core import request_backend_policy as policy
from gc_core.execution.finish_reason import nonempty_reasoning_text
from gc_core.request_backend_policy import _raise_on_embedded_choice_error
from gc_core.utils.backend_errors import (
    EmbeddedChoiceError,
    is_empty_completion,
)
from gc_core.utils.convertations import get_output_list
from gc_core.utils.format_messages import (
    format_message_chat_completions_to_send_backend,
)
from gc_testkit import run_coro


def completion(content, reasoning, *, finish="stop", tools=None):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning,
                    "tool_calls": tools,
                },
            }
        ],
    }


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("content", ["  answer\n\n", "\n\n"])
def test_content_survives_conversion_and_outbound_formatting(persist, content):
    output = get_output_list(
        completion(content, None), persist_reasoning_content=persist
    )
    assert output[0]["content"][0]["text"] == content
    outbound = format_message_chat_completions_to_send_backend(output[0])
    assert outbound["content"] == content


@pytest.mark.parametrize("reasoning", ["  reason\n", "  \n"])
def test_completed_reasoning_retains_original_whitespace(reasoning):
    output = get_output_list(
        completion("\n\n", reasoning), persist_reasoning_content=True
    )
    assert output[0]["content"][0]["text"] == "\n\n"
    assert output[0]["reasoning_content"] == reasoning
    outbound = format_message_chat_completions_to_send_backend(output[0])
    assert outbound["reasoning_content"] == reasoning


def test_salvage_keeps_real_reasoning_and_original_content_whitespace():
    output = get_output_list(
        completion("\n\n", "  search the leaderboard\n", finish="length"),
        persist_reasoning_content=False,
    )
    assert output[0]["content"][0]["text"] == (
        "<think>  search the leaderboard\n</think>\n\n"
    )


@pytest.mark.parametrize("persist", [False, True])
def test_whitespace_content_survives_tool_turn(persist):
    tools = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "example", "arguments": '{"value":0.0}'},
        }
    ]
    output = get_output_list(
        completion("\n\n", "reason", finish="tool_calls", tools=tools),
        persist_reasoning_content=persist,
    )
    assert output[0]["content"][0]["text"] == "\n\n"
    assert output[1]["arguments"] == '{"value":0.0}'


@pytest.mark.parametrize(
    ("primary", "alias", "expected"),
    [
        ("\n\n", "  REAL CoT\n", "  REAL CoT\n"),
        ("  primary\n", "secondary", "  primary\n"),
        ("\n\n", "  ", "\n\n"),
    ],
)
def test_alias_selection_preserves_selected_original_text(
    primary, alias, expected
):
    assert (
        nonempty_reasoning_text(
            {"reasoning_content": primary, "reasoning": alias}
        )
        == expected
    )


def test_incomplete_whitespace_reasoning_is_not_usable_payload():
    assert is_empty_completion(completion(None, "  \n", finish=None)) is True


def test_error_with_whitespace_reasoning_is_raised_by_main_agent_policy():
    with pytest.raises(EmbeddedChoiceError):
        _raise_on_embedded_choice_error(
            completion(None, "\n", finish="error"), salvage_reasoning=True
        )


def test_training_mode_preserves_inline_think_and_trailing_lf():
    content = "<think>t</think>\n\nAnswer\n"
    output = get_output_list(
        completion(content, None), persist_reasoning_content=True
    )
    assert output[0]["content"][0]["text"] == content


def test_policy_retries_error_with_whitespace_reasoning_then_succeeds(
    monkeypatch,
):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(policy.asyncio, "sleep", no_sleep)
    attempts = []

    async def request(payload, *_args, **_kwargs):
        attempts.append(payload)
        if len(attempts) == 1:
            return completion(None, "\n", finish="error")
        return completion("  recovered\n\n", None)

    monkeypatch.setattr(policy, "arequest_backend_chat_completions", request)
    response, _ = run_coro(
        policy.arequest_chat_completions_with_policy(
            payload_factory=lambda mt: {"max_tokens": mt},
            initial_max_tokens=8192,
            min_response_tokens=1024,
            headers={},
            url="http://x",
            verify=True,
            use_retry=False,
            salvage_reasoning=True,
        )
    )
    assert len(attempts) == 2
    assert response["choices"][0]["message"]["content"] == "  recovered\n\n"

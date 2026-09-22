from gc_core.security.log_redaction import MASK, Redactor, build_redactor


def test_value_patterns_masked():
    r = Redactor()
    assert MASK in r.redact("auth Bearer abc.def-123")
    assert "abc.def-123" not in r.redact("auth Bearer abc.def-123")
    assert MASK in r.redact("eyJhbG.eyJ123.sig_AB")
    assert MASK in r.redact("key sk-ABCDEF0123456789xyz here")
    assert MASK in r.redact("AKIAABCDEFGHIJKLMNOP")
    assert MASK in r.redact("Basic dXNlcjpwYXNz")


def test_github_and_vendor_tokens_masked():
    r = Redactor()
    secrets = [
        "ghp_FAKE1234567890abcdefABCDEF1234567890",  # GitHub PAT
        "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",  # GitHub OAuth
        "github_pat_11ABCDEFG0aBcDeFgHiJkL_mnopAB",  # fine-grained PAT
        "sk_live_ABCDEF0123456789abcdef",  # Stripe
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",  # Google API key
        "ya29.A0ARrdaMabcdef_GHIJKL-0123456789",  # Google OAuth
        "glpat-ABCDEFGHIJ0123456789",  # GitLab PAT
        "npm_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",  # npm
        "xoxb-99887766-SECRETTAILxyz",  # Slack
    ]
    for s in secrets:
        out = r.redact(f"value: {s} end")
        assert s not in out, f"not masked: {s}"
        assert MASK in out


def test_pem_private_key_masked():
    r = Redactor()
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n"
        "AAAAAAAQABAAAB\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    out = r.redact({"key": pem, "note": f"my key:\n{pem}\nok"})
    assert out["key"] == MASK  # значение под чувствительным ключом
    assert "BEGIN OPENSSH PRIVATE KEY" not in out["note"]
    assert MASK in out["note"]


def test_no_false_positive_on_plain_text():
    r = Redactor()
    text = "the cat sat on the mat with 12345 items and id abc-def"
    assert r.redact(text) == text


def test_sensitive_keys_masked():
    r = Redactor()
    out = r.redact({"Authorization": "Bearer x", "X-Api-Key": "secret123", "model": "qwen"})
    assert out["Authorization"] == MASK
    assert out["X-Api-Key"] == MASK
    assert out["model"] == "qwen"


def test_empty_sensitive_value_not_masked():
    r = Redactor()
    out = r.redact({"token": "", "password": None})
    assert out["token"] == ""
    assert out["password"] is None


def test_nested_structures_and_scalars():
    r = Redactor()
    out = r.redact({"a": [{"content": "Bearer zzz"}], "n": 5, "b": True})
    assert MASK in out["a"][0]["content"]
    assert out["n"] == 5
    assert out["b"] is True


def test_non_mutating():
    r = Redactor()
    src = {"token": "live"}
    r.redact(src)
    assert src["token"] == "live"


def test_extra_keys_and_patterns():
    r = Redactor(extra_keys=["session_id"], extra_patterns=[r"CUSTOM-[0-9]+"])
    out = r.redact({"session_id": "abc", "note": "CUSTOM-99 tail"})
    assert out["session_id"] == MASK
    assert MASK in out["note"]


def test_bad_extra_pattern_skipped():
    r = Redactor(extra_patterns=["("])  # invalid regex
    assert r.redact("plain") == "plain"  # did not raise


def test_token_count_keys_not_masked():
    """prompt_tokens / completion_tokens / max_tokens — счётчики, НЕ секреты."""
    r = Redactor()
    out = r.redact(
        {
            "prompt_tokens": 123,
            "completion_tokens": 45,
            "total_tokens": 168,
            "max_tokens": 32768,
            "tokenizer": "bpe",
        }
    )
    assert out["prompt_tokens"] == 123
    assert out["completion_tokens"] == 45
    assert out["total_tokens"] == 168
    assert out["max_tokens"] == 32768
    assert out["tokenizer"] == "bpe"


def test_token_word_keys_masked():
    """Одиночное слово token / *_token — секрет, маскируем."""
    r = Redactor()
    out = r.redact({"token": "abc", "access_token": "xyz", "auth_token": "q", "x-token": "w"})
    assert out["token"] == MASK
    assert out["access_token"] == MASK
    assert out["auth_token"] == MASK
    assert out["x-token"] == MASK


def test_build_redactor_toggle():
    assert build_redactor(None) is None
    assert build_redactor({"enabled": False}) is None
    assert isinstance(build_redactor({"enabled": True}), Redactor)

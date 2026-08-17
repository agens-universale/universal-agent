from gc_cli import onboarding_wizard as wz


def test_bare_key_gets_bearer():
    assert wz.authorization_header("sk-abc123") == "Bearer sk-abc123"
    assert wz.authorization_header("  sk-1  ") == "Bearer sk-1"


def test_value_with_scheme_is_verbatim():
    assert wz.authorization_header("Basic zzz") == "Basic zzz"
    assert wz.authorization_header("Bearer sk-1") == "Bearer sk-1"


def test_mask():
    assert wz.mask_authorization("Bearer sk-abc123") == "Bearer sk-***"
    assert wz.mask_authorization("***") == "***"

def test_auth_helpers_live_in_core():
    from gc_core import onboarding
    assert onboarding.authorization_header("sk") == "Bearer sk"
    assert onboarding.authorization_header("Basic abc") == "Basic abc"
    assert onboarding.mask_authorization("Bearer secret").startswith("Bearer sec")


def test_onboarding_fields_shape():
    from gc_core import onboarding_fields as f
    assert [fs.key for fs in f.ONBOARDING_FIELDS] == ["completions_url", "Authorization", "model"]
    assert f.ONBOARDING_FIELDS[0].label == "Адрес сервера"
    assert "http" in f.ONBOARDING_FIELDS[0].hint
    assert len(f.AUTH_OPTIONS) == 3
    assert "инструменты (tools)" in f.MODEL_WARN

def test_backend_config_tls_fields():
    from config_modules.backend import BackendConfig
    c = BackendConfig(use_ssl="/ca.pem", cert_path="/c.crt", key_path="/k.key")
    assert c.use_ssl == "/ca.pem"
    assert c.cert_path == "/c.crt"
    assert c.key_path == "/k.key"
    # backward compat: bool use_ssl + optional cert
    assert BackendConfig().use_ssl is True
    assert BackendConfig().cert_path is None
    assert BackendConfig(use_ssl=False).use_ssl is False


def test_config_dict_maps_backend_tls(tmp_path):
    import yaml
    from config import load_config
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump({"backend": {
        "api_mode": "chat_completions", "use_ssl": "/ca.pem",
        "cert_path": "/c.crt", "key_path": "/k.key"}}), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["use_ssl"] == "/ca.pem"
    assert cfg["cert_path"] == "/c.crt"
    assert cfg["key_path"] == "/k.key"


def test_backend_config_default_use_ssl_bool_maps(tmp_path):
    import yaml
    from config import load_config
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump({"backend": {"api_mode": "chat_completions"}}), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["use_ssl"] is True
    assert cfg["cert_path"] is None
    assert cfg["key_path"] is None


def test_get_backend_client_cert():
    from utils.ssl_config import get_backend_client_cert
    assert get_backend_client_cert({"cert_path": "/c", "key_path": "/k"}) == ("/c", "/k")
    assert get_backend_client_cert({"cert_path": "/c"}) is None       # only one → None
    assert get_backend_client_cert({}) is None

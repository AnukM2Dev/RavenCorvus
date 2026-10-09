import pytest

from ha_mcp.config import DEFAULT_SENSITIVE_DOMAINS, ConfigError, Settings

ENV_KEYS = [
    "HASS_URL",
    "HASS_TOKEN",
    "HASS_VERIFY_TLS",
    "HASS_TIMEOUT",
    "HASS_READ_ONLY",
    "HASS_ALLOWED_DOMAINS",
    "HASS_DENIED_ENTITIES",
    "HASS_ALLOW_SENSITIVE_DOMAINS",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_requires_url_and_token(monkeypatch):
    with pytest.raises(ConfigError, match="HASS_URL"):
        Settings.from_env()
    monkeypatch.setenv("HASS_URL", "http://ha:8123")
    with pytest.raises(ConfigError, match="HASS_TOKEN"):
        Settings.from_env()


def test_rejects_url_without_scheme(monkeypatch):
    monkeypatch.setenv("HASS_URL", "ha.local:8123")
    monkeypatch.setenv("HASS_TOKEN", "t")
    with pytest.raises(ConfigError, match="http"):
        Settings.from_env()


def test_defaults_are_safe_but_permissive_for_ordinary_domains(monkeypatch):
    monkeypatch.setenv("HASS_URL", "http://ha:8123/")
    monkeypatch.setenv("HASS_TOKEN", "t")
    s = Settings.from_env()
    assert s.url == "http://ha:8123"  # trailing slash stripped
    assert s.verify_tls and not s.read_only
    assert s.allow_sensitive == frozenset()
    assert s.sensitive_domains == DEFAULT_SENSITIVE_DOMAINS


def test_parses_all_knobs(monkeypatch):
    monkeypatch.setenv("HASS_URL", "https://ha")
    monkeypatch.setenv("HASS_TOKEN", "t")
    monkeypatch.setenv("HASS_VERIFY_TLS", "0")
    monkeypatch.setenv("HASS_TIMEOUT", "2.5")
    monkeypatch.setenv("HASS_READ_ONLY", "Yes")
    monkeypatch.setenv("HASS_ALLOWED_DOMAINS", "light, Switch ,")
    monkeypatch.setenv("HASS_DENIED_ENTITIES", "lock.*,Switch.Server")
    monkeypatch.setenv("HASS_ALLOW_SENSITIVE_DOMAINS", "climate,cover")
    s = Settings.from_env()
    assert not s.verify_tls and s.timeout == 2.5 and s.read_only
    assert s.allowed_domains == {"light", "switch"}
    assert s.denied_entities == {"lock.*", "switch.server"}
    assert s.allow_sensitive == {"climate", "cover"}


@pytest.mark.parametrize("value", ["1", "true", "all"])
def test_allow_sensitive_blanket_values(monkeypatch, value):
    monkeypatch.setenv("HASS_URL", "http://ha")
    monkeypatch.setenv("HASS_TOKEN", "t")
    monkeypatch.setenv("HASS_ALLOW_SENSITIVE_DOMAINS", value)
    assert Settings.from_env().allow_sensitive == DEFAULT_SENSITIVE_DOMAINS


def test_bad_timeout(monkeypatch):
    monkeypatch.setenv("HASS_URL", "http://ha")
    monkeypatch.setenv("HASS_TOKEN", "t")
    monkeypatch.setenv("HASS_TIMEOUT", "soon")
    with pytest.raises(ConfigError, match="HASS_TIMEOUT"):
        Settings.from_env()


def test_token_never_appears_in_repr():
    s = Settings(url="http://ha", token="super-secret")
    assert "super-secret" not in repr(s)

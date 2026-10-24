import json

import httpx
import pytest

from ha_mcp.config import ConfigError
from ha_mcp.http_transport import AccessVerifier, HttpSettings, KeyGate, build_http_app
from ha_mcp.server import build_server

KEY = "k" * 40


# --- settings ---------------------------------------------------------------


def test_settings_require_key():
    with pytest.raises(ConfigError, match="RAVEN_API_KEY is required"):
        HttpSettings.from_env({})


def test_settings_reject_short_key():
    with pytest.raises(ConfigError, match="at least"):
        HttpSettings.from_env({"RAVEN_API_KEY": "short"})


def test_settings_reject_unsafe_key():
    with pytest.raises(ConfigError, match="URL-safe"):
        HttpSettings.from_env({"RAVEN_API_KEY": "a/" + "b" * 40})


def test_settings_defaults_and_overrides():
    s = HttpSettings.from_env({"RAVEN_API_KEY": KEY})
    assert (s.host, s.port, s.path, s.allowed_hosts) == ("0.0.0.0", 8765, "/mcp", [])
    assert s.path_key is False and s.access_enabled is False
    s = HttpSettings.from_env(
        {"RAVEN_API_KEY": KEY, "RAVEN_PORT": "9000", "RAVEN_ALLOWED_HOSTS": "a.com, b.com"}
    )
    assert s.port == 9000 and s.allowed_hosts == ["a.com", "b.com"]


def test_settings_access_needs_both_values():
    with pytest.raises(ConfigError, match="both"):
        HttpSettings.from_env({"RAVEN_API_KEY": KEY, "RAVEN_ACCESS_TEAM": "ravenswood"})


@pytest.mark.parametrize(
    "team", ["ravenswood", "ravenswood.cloudflareaccess.com", "https://ravenswood.cloudflareaccess.com/"]
)
def test_settings_access_issuer_normalized(team):
    s = HttpSettings.from_env({"RAVEN_API_KEY": KEY, "RAVEN_ACCESS_TEAM": team, "RAVEN_ACCESS_AUD": "a"})
    assert s.access_enabled and s.access_issuer == "https://ravenswood.cloudflareaccess.com"


def test_settings_path_key_opt_in():
    assert HttpSettings.from_env({"RAVEN_API_KEY": KEY, "RAVEN_PATH_KEY": "1"}).path_key


def test_settings_bad_port():
    with pytest.raises(ConfigError, match="RAVEN_PORT"):
        HttpSettings.from_env({"RAVEN_API_KEY": KEY, "RAVEN_PORT": "x"})


# --- gate (against a stub app) ----------------------------------------------


async def echo_app(scope, receive, send):
    body = json.dumps({"path": scope["path"]}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def gate_client(**kw):
    kw.setdefault("path_key", True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=KeyGate(echo_app, KEY, **kw)), base_url="http://t")


async def test_healthz_is_open():
    async with gate_client() as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/mcp", {}),
        ("/mcp", {"Authorization": "Bearer wrong"}),
        ("/mcp", {"Authorization": KEY}),  # missing scheme
        ("/wrong/mcp", {}),
        (f"/{KEY}x/mcp", {}),
        (f"/x{KEY}/mcp", {}),
    ],
)
async def test_gate_rejects(path, headers):
    async with gate_client() as c:
        r = await c.post(path, headers=headers)
    assert r.status_code == 401


async def test_401_carries_www_authenticate():
    async with gate_client() as c:
        r = await c.post("/mcp")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


async def test_path_key_off_by_default():
    async with gate_client(path_key=False) as c:
        r = await c.post(f"/{KEY}/mcp")
    assert r.status_code == 401


async def test_gate_accepts_bearer_and_keeps_path():
    async with gate_client() as c:
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200 and r.json()["path"] == "/mcp"


async def test_gate_accepts_path_prefix_and_strips_it():
    async with gate_client() as c:
        r = await c.post(f"/{KEY}/mcp")
    assert r.status_code == 200 and r.json()["path"] == "/mcp"


# --- end to end through the real MCP app -------------------------------------

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc_result(r):
    text = r.text
    if "data:" in text:  # SSE framing
        text = next(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
    return json.loads(text)


from contextlib import asynccontextmanager


@asynccontextmanager
async def running(app):
    """Run the Starlette lifespan once (the session manager starts once) and yield a poster."""
    inner = app.app if isinstance(app, KeyGate) else app
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.example.com"
        ) as c:

            async def post(path, body, headers=None):
                return await c.post(path, json=body, headers={**MCP_HEADERS, **(headers or {})})

            yield post


async def test_end_to_end_initialize_and_list_tools(make_ops):
    app = build_http_app(build_server(make_ops()), HttpSettings(api_key=KEY, path_key=True))

    async with running(app) as post:
        r = await post("/mcp", INIT)
        assert r.status_code == 401

        r = await post(f"/{KEY}/mcp", INIT)
        assert r.status_code == 200, r.text
        assert _rpc_result(r)["result"]["serverInfo"]

        r = await post(
            "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"Authorization": f"Bearer {KEY}"},
        )
        assert r.status_code == 200, r.text
        names = {t["name"] for t in _rpc_result(r)["result"]["tools"]}
        assert "call_service" in names and "get_state" in names


async def test_allowed_hosts_enforced(make_ops):
    app = build_http_app(
        build_server(make_ops()), HttpSettings(api_key=KEY, path_key=True, allowed_hosts=["mcp.good.com"])
    )
    async with running(app) as post:
        r = await post(f"/{KEY}/mcp", INIT)  # Host: mcp.example.com
    assert r.status_code in (400, 421)


# --- Cloudflare Access JWT ---------------------------------------------------

ISSUER = "https://ravenswood.cloudflareaccess.com"
AUD = "aud-tag-123"


@pytest.fixture(scope="module")
def rsa_keys():
    from cryptography.hazmat.primitives.asymmetric import rsa

    good = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return good, bad


class _StaticJWKS:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.public_key})()


def _token(private_key, **over):
    import time

    import jwt

    claims = {"iss": ISSUER, "aud": [AUD], "iat": int(time.time()), "exp": int(time.time()) + 300,
              "email": "mike@example.com"}
    claims.update(over)
    return jwt.encode(claims, private_key, algorithm="RS256")


def access_client(rsa_keys):
    good, _ = rsa_keys
    verifier = AccessVerifier(ISSUER, AUD, jwks_client=_StaticJWKS(good.public_key()))
    return gate_client(path_key=False, verifier=verifier)


async def test_access_jwt_accepted(rsa_keys):
    async with access_client(rsa_keys) as c:
        r = await c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": _token(rsa_keys[0])})
    assert r.status_code == 200 and r.json()["path"] == "/mcp"


@pytest.mark.parametrize(
    "case",
    ["wrong_key", "wrong_aud", "wrong_iss", "expired", "garbage"],
)
async def test_access_jwt_rejected(rsa_keys, case):
    import time

    good, bad = rsa_keys
    token = {
        "wrong_key": lambda: _token(bad),
        "wrong_aud": lambda: _token(good, aud=["other"]),
        "wrong_iss": lambda: _token(good, iss="https://evil.cloudflareaccess.com"),
        "expired": lambda: _token(good, iat=int(time.time()) - 900, exp=int(time.time()) - 600),
        "garbage": lambda: "not.a.jwt",
    }[case]()
    async with access_client(rsa_keys) as c:
        r = await c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": token})
    assert r.status_code == 401


async def test_bearer_still_works_with_access_enabled(rsa_keys):
    async with access_client(rsa_keys) as c:
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200

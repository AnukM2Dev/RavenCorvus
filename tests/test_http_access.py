import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ha_mcp.config import ConfigError
from ha_mcp.http import AccessVerifier, AuthMiddleware, HttpAuthConfig

ISSUER = "https://ravenswood.cloudflareaccess.com"
AUD = "aud-tag-123"
SECRET = "s3cretpathsecret_abcdefghijklmnop"
TOKEN = "bearer-token-0123456789abcdef"


@pytest.fixture(scope="module")
def keys():
    good = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return good, bad


class StaticJWKS:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.public_key})()


def tok(private_key, **over):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": [AUD], "iat": now, "exp": now + 300, "email": "m@example.com"}
    claims.update(over)
    return jwt.encode(claims, private_key, algorithm="RS256")


async def echo(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": scope["path"].encode()})


def client(keys, **cfg):
    cfg.setdefault("access_team", "ravenswood")
    cfg.setdefault("access_aud", AUD)
    c = HttpAuthConfig(**cfg)
    c.validate()
    verifier = AccessVerifier(ISSUER, AUD, jwks_client=StaticJWKS(keys[0].public_key()))
    app = AuthMiddleware(echo, c, verifier)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# --- config -----------------------------------------------------------------


def test_access_alone_satisfies_mandatory_auth():
    HttpAuthConfig(access_team="ravenswood", access_aud=AUD).validate()


def test_access_needs_both_values():
    with pytest.raises(ConfigError, match="both"):
        HttpAuthConfig(access_team="ravenswood").validate()


@pytest.mark.parametrize(
    "team", ["ravenswood", "ravenswood.cloudflareaccess.com", "https://ravenswood.cloudflareaccess.com/"]
)
def test_issuer_normalized(team):
    assert HttpAuthConfig(access_team=team, access_aud="a").access_issuer == ISSUER


def test_from_env_reads_access(monkeypatch):
    monkeypatch.setenv("HA_MCP_ACCESS_TEAM", "ravenswood")
    monkeypatch.setenv("HA_MCP_ACCESS_AUD", AUD)
    cfg = HttpAuthConfig.from_env()
    assert cfg.access_enabled and cfg.access_aud == AUD


# --- Access-only deployment ---------------------------------------------------


async def test_access_only_accepts_valid_token(keys):
    async with client(keys) as c:
        r = await c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": tok(keys[0])})
    assert r.status_code == 200 and r.text == "/mcp"


async def test_access_only_rejects_missing_token(keys):
    async with client(keys) as c:
        r = await c.post("/mcp")
    assert r.status_code == 401 and "www-authenticate" in r.headers


@pytest.mark.parametrize("case", ["wrong_key", "wrong_aud", "wrong_iss", "expired", "garbage"])
async def test_access_rejects_bad_tokens(keys, case):
    good, bad = keys
    now = int(time.time())
    token = {
        "wrong_key": lambda: tok(bad),
        "wrong_aud": lambda: tok(good, aud=["other"]),
        "wrong_iss": lambda: tok(good, iss="https://evil.cloudflareaccess.com"),
        "expired": lambda: tok(good, iat=now - 900, exp=now - 600),
        "garbage": lambda: "not.a.jwt",
    }[case]()
    async with client(keys) as c:
        r = await c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": token})
    assert r.status_code == 401


# --- Access alongside the existing secrets ------------------------------------


async def test_access_token_skips_path_secret(keys):
    async with client(keys, path_secret=SECRET) as c:
        r = await c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": tok(keys[0])})
    assert r.status_code == 200 and r.text == "/mcp"


async def test_access_token_with_old_secret_url_still_routes(keys):
    async with client(keys, path_secret=SECRET) as c:
        r = await c.post(f"/{SECRET}/mcp", headers={"Cf-Access-Jwt-Assertion": tok(keys[0])})
    assert r.status_code == 200 and r.text == "/mcp"


async def test_without_access_token_path_secret_still_required(keys):
    async with client(keys, path_secret=SECRET) as c:
        assert (await c.post("/mcp")).status_code == 404
        assert (await c.post(f"/{SECRET}/mcp")).status_code == 200


async def test_bearer_still_works_with_access(keys):
    async with client(keys, token=TOKEN) as c:
        assert (await c.post("/mcp")).status_code == 401
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200

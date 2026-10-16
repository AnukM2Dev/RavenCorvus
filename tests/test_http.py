import httpx
import pytest

from ha_mcp.config import ConfigError
from ha_mcp.http import AuthMiddleware, HttpAuthConfig, build_app
from ha_mcp.server import build_server

SECRET = "s3cretpathsecret_abcdefghijklmnop"
TOKEN = "bearer-token-0123456789abcdef"

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}
HDRS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


def test_refuses_open_endpoint():
    with pytest.raises(ConfigError, match="authentication"):
        HttpAuthConfig().validate()


def test_rejects_weak_secrets():
    with pytest.raises(ConfigError):
        HttpAuthConfig(path_secret="short").validate()
    with pytest.raises(ConfigError):
        HttpAuthConfig(token="short").validate()


def test_from_env(monkeypatch):
    monkeypatch.setenv("HA_MCP_PATH_SECRET", f"/{SECRET}/")
    monkeypatch.setenv("HA_MCP_ALLOWED_HOSTS", "a.example.com, b.example.com")
    cfg = HttpAuthConfig.from_env()
    assert cfg.path_secret == SECRET and cfg.allowed_hosts == ["a.example.com", "b.example.com"]


class Echo:
    """Records the path the inner app saw."""

    seen = None

    async def __call__(self, scope, receive, send):
        Echo.seen = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def call(app, path, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(path, headers=headers or {})


async def test_path_secret_gate_and_strip():
    app = AuthMiddleware(Echo(), HttpAuthConfig(path_secret=SECRET))
    assert (await call(app, "/mcp")).status_code == 404
    assert (await call(app, "/wrong/mcp")).status_code == 404
    assert (await call(app, "/")).status_code == 404
    r = await call(app, f"/{SECRET}/mcp")
    assert r.status_code == 200 and Echo.seen == "/mcp"


async def test_bearer_required():
    app = AuthMiddleware(Echo(), HttpAuthConfig(token=TOKEN))
    assert (await call(app, "/mcp")).status_code == 401
    assert (await call(app, "/mcp", {"authorization": "Bearer nope"})).status_code == 401
    assert (await call(app, "/mcp", {"authorization": TOKEN})).status_code == 401
    r = await call(app, "/mcp", {"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


async def test_both_checks_apply():
    app = AuthMiddleware(Echo(), HttpAuthConfig(token=TOKEN, path_secret=SECRET))
    assert (await call(app, f"/{SECRET}/mcp")).status_code == 401
    assert (await call(app, "/mcp", {"authorization": f"Bearer {TOKEN}"})).status_code == 404
    ok = await call(app, f"/{SECRET}/mcp", {"authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


async def test_real_mcp_initialize_through_secret_path(make_ops):
    cfg = HttpAuthConfig(path_secret=SECRET)
    app = build_app(build_server(make_ops(read_only=True)), cfg)
    transport = httpx.ASGITransport(app=app)
    # httpx does not run ASGI lifespan, so start the MCP session manager explicitly
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=transport, base_url="http://tunnel.example.com") as c:
            assert (await c.post("/mcp", json=INIT, headers=HDRS)).status_code == 404
            r = await c.post(f"/{SECRET}/mcp", json=INIT, headers=HDRS)
    assert r.status_code == 200, r.text
    assert "homeassistant" in r.text

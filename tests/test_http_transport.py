import json

import httpx
import pytest

from ha_mcp.config import ConfigError
from ha_mcp.http_transport import HttpSettings, KeyGate, build_http_app
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
    s = HttpSettings.from_env(
        {"RAVEN_API_KEY": KEY, "RAVEN_PORT": "9000", "RAVEN_ALLOWED_HOSTS": "a.com, b.com"}
    )
    assert s.port == 9000 and s.allowed_hosts == ["a.com", "b.com"]


def test_settings_bad_port():
    with pytest.raises(ConfigError, match="RAVEN_PORT"):
        HttpSettings.from_env({"RAVEN_API_KEY": KEY, "RAVEN_PORT": "x"})


# --- gate (against a stub app) ----------------------------------------------


async def echo_app(scope, receive, send):
    body = json.dumps({"path": scope["path"]}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def gate_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=KeyGate(echo_app, KEY)), base_url="http://t")


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
    app = build_http_app(build_server(make_ops()), HttpSettings(api_key=KEY))

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
        build_server(make_ops()), HttpSettings(api_key=KEY, allowed_hosts=["mcp.good.com"])
    )
    async with running(app) as post:
        r = await post(f"/{KEY}/mcp", INIT)  # Host: mcp.example.com
    assert r.status_code in (400, 421)

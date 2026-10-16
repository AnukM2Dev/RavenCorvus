import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from ha_mcp import watch
from ha_mcp.config import ConfigError


class FakeClient:
    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, data=None, **kw):
        self.calls.append((domain, service, data))


class FakeOps:
    def __init__(self):
        self.client = FakeClient()

    async def list_entities(self, **kw):
        return {"entities": [{"entity_id": "lock.front", "state": "unlocked"}]}

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on"}

    async def get_error_log(self, lines=100):
        return "x" * 50000


def cfg(tmp_path, notify="notify.mobile_app_pixel"):
    return watch.WatchConfig(api_key="sk-ant-test", notify_service=notify, data_dir=tmp_path)


def api_transport(replies):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["x-api-key"] == "sk-ant-test"
        return httpx.Response(200, json={"content": replies.pop(0)})

    return httpx.MockTransport(handler), seen


def test_parse_verdict():
    v = watch.parse_verdict('thinking {"x":1} done {"notify": true, "title": "Door", "message": "Open"}')
    assert v == {"notify": True, "title": "Door", "message": "Open"}
    assert watch.parse_verdict("nothing") == {"notify": False, "title": "", "message": ""}


def test_config_validation(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        watch.WatchConfig.from_env()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("WATCH_NOTIFY_SERVICE", "light.turn_on")
    with pytest.raises(ConfigError):
        watch.WatchConfig.from_env()
    monkeypatch.setenv("WATCH_NOTIFY_SERVICE", "notify.mobile_app_p")
    monkeypatch.setenv("WATCH_TIMES", "7:30, 19:00")
    assert watch.WatchConfig.from_env().watch_times == [(7, 30), (19, 0)]


async def test_unknown_and_truncated_tools():
    ops = FakeOps()
    assert "Unknown tool" in await watch.run_tool(ops, "call_service", {})
    out = await watch.run_tool(ops, "get_error_log", {})
    assert out.endswith("narrow your query]") and len(out) < 13000


async def test_watch_notifies_and_records(tmp_path):
    ops = FakeOps()
    replies = [
        [{"type": "tool_use", "id": "t1", "name": "list_entities", "input": {"domain": "lock"}}],
        [{"type": "text", "text": '{"notify": true, "title": "Lock", "message": "Front lock unlocked."}'}],
    ]
    transport, seen = api_transport(replies)
    now = datetime(2026, 10, 16, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    async with httpx.AsyncClient(transport=transport) as http:
        v = await watch.run_watch(cfg(tmp_path), ops, http, now)
    assert v["notify"] and v["sent"]
    assert ops.client.calls == [("notify", "mobile_app_pixel", {"title": "Lock", "message": "Front lock unlocked."})]
    assert seen[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "lock.front" in seen[1]["messages"][-1]["content"][0]["content"]
    # tools offered to the model are read-only
    assert {t["name"] for t in seen[0]["tools"]} <= {
        "list_entities", "get_state", "search_entities", "list_areas", "get_history", "get_logbook", "get_error_log"}
    # the alert is fed back next time to avoid repeats
    assert json.loads((tmp_path / "recent_alerts.json").read_text())[0]["title"] == "Lock"


async def test_watch_quiet_sends_nothing(tmp_path):
    ops = FakeOps()
    transport, _ = api_transport([[{"type": "text", "text": '{"notify": false}'}]])
    now = datetime(2026, 10, 16, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    async with httpx.AsyncClient(transport=transport) as http:
        v = await watch.run_watch(cfg(tmp_path), ops, http, now)
    assert not v["notify"] and ops.client.calls == []


async def test_nightly_writes_memory(tmp_path):
    ops = FakeOps()
    transport, _ = api_transport([[{"type": "text", "text": "- Garage door opens ~7am weekdays"}]])
    now = datetime(2026, 10, 16, 3, 0, tzinfo=ZoneInfo("America/New_York"))
    async with httpx.AsyncClient(transport=transport) as http:
        await watch.run_nightly(cfg(tmp_path), ops, http, now)
    assert "Garage door" in (tmp_path / "house_memory.md").read_text()


async def test_api_error_surfaces(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"}))
    now = datetime(2026, 10, 16, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(RuntimeError, match="401"):
            await watch.run_watch(cfg(tmp_path), FakeOps(), http, now)


def test_next_event_schedule():
    c = watch.WatchConfig(api_key="k")
    tz = ZoneInfo("America/New_York")
    assert watch.next_event(datetime(2026, 10, 16, 7, 0, tzinfo=tz), c)[1:] == ("watch",)
    assert watch.next_event(datetime(2026, 10, 16, 19, 0, tzinfo=tz), c) == (
        datetime(2026, 10, 17, 3, 0, tzinfo=tz), "nightly")


def test_base_url_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com/")
    assert watch.WatchConfig.from_env().messages_url == "https://proxy.example.com/v1/messages"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://insecure")
    with pytest.raises(ConfigError):
        watch.WatchConfig.from_env()

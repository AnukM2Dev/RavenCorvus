import httpx
import pytest

from ha_mcp.client import (
    HAAuthError,
    HABadRequestError,
    HAClient,
    HAConnectionError,
    HAError,
    HANotFoundError,
    HAServerError,
)

from .conftest import make_settings


async def test_status_reports_version_and_policy(make_client):
    client = make_client(read_only=True)
    st = await client.status()
    assert st["ok"] and st["version"] == "2026.10.0"
    assert st["components"] == 3
    assert st["read_only"] is True


async def test_sends_bearer_header(ha, make_client):
    await make_client().status()
    assert ha.requests[0].headers["authorization"] == "Bearer test-token"


async def test_get_state_and_404(make_client):
    client = make_client()
    assert (await client.get_state("light.kitchen"))["state"] == "on"
    with pytest.raises(HANotFoundError):
        await client.get_state("light.nope")


@pytest.mark.parametrize(
    "code, exc",
    [(401, HAAuthError), (403, HAAuthError), (400, HABadRequestError), (500, HAServerError), (418, HAError)],
)
async def test_http_status_mapping(ha, make_client, code, exc):
    ha.fail_with = code
    with pytest.raises(exc):
        await make_client().status()


async def test_bad_token_gives_actionable_auth_error(ha, make_client):
    ha.token = "something-else"
    with pytest.raises(HAAuthError, match="HASS_TOKEN"):
        await make_client().status()


async def test_connection_error_is_typed():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client = HAClient(make_settings(), transport=httpx.MockTransport(boom))
    with pytest.raises(HAConnectionError, match="Cannot reach"):
        await client.status()
    await client.aclose()


async def test_timeout_is_typed():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = HAClient(make_settings(timeout=3), transport=httpx.MockTransport(slow))
    with pytest.raises(HAConnectionError, match="Timed out after 3"):
        await client.status()
    await client.aclose()


async def test_call_service_returns_changed_states(ha, make_client):
    result = await make_client().call_service("light", "turn_off", {"entity_id": "light.kitchen"})
    assert [s["entity_id"] for s in result["changed_states"]] == ["light.kitchen"]
    assert result["service_response"] is None
    assert ha.states["light.kitchen"]["state"] == "off"


async def test_call_service_return_response_shape(make_client):
    result = await make_client().call_service(
        "light", "turn_off", {"entity_id": "light.kitchen"}, return_response=True
    )
    assert result["service_response"] == {"ok": True}
    assert len(result["changed_states"]) == 1


async def test_unknown_service_is_404(make_client):
    with pytest.raises(HANotFoundError):
        await make_client().call_service("light", "explode", {})


async def test_entity_ids_are_url_quoted(ha, make_client):
    with pytest.raises(HANotFoundError):
        await make_client().get_state("light/../config")
    assert "%2F" in str(ha.requests[-1].url)


async def test_history_builds_period_url_and_minimal_flags(ha, make_client):
    await make_client().get_history(["light.kitchen", "light.bedroom"], hours=2)
    req = ha.requests[-1]
    assert req.url.path.startswith("/api/history/period/")
    assert req.url.params["filter_entity_id"] == "light.kitchen,light.bedroom"
    assert "minimal_response" in req.url.params and "no_attributes" in req.url.params


async def test_render_template_and_error_log(make_client):
    c = make_client()
    assert await c.render_template("{{ 1 + 1 }}") == "rendered:{{ 1 + 1 }}"
    assert (await c.get_error_log()).startswith("line 0")


# ------------------------------------------------------------------ websocket
async def test_registries_over_one_websocket_session(ha, make_client):
    reg = await make_client().registries()
    assert [a["area_id"] for a in reg["areas"]] == ["kitchen", "bedroom"]
    assert ha.ws_commands == [
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
    ]
    assert ha.ws_url == "ws://ha.test:8123/api/websocket"


async def test_entity_area_map_inherits_device_area_with_override(make_client):
    m = await make_client().entity_area_map()
    assert m["light.kitchen"] == {"area_id": "kitchen", "area": "Kitchen"}  # via device
    assert m["light.bedroom"]["area"] == "Bedroom"  # direct
    assert "sensor.outdoor_temp" not in m


async def test_ws_auth_failure(ha, make_client):
    ha.token = "other"
    with pytest.raises(HAAuthError, match="WebSocket auth failed"):
        await make_client().list_areas()


async def test_ws_command_failure_surfaces_code(make_client):
    with pytest.raises(HAError, match="unknown_command"):
        await make_client().ws_command("config/nope")


async def test_ws_connect_failure_is_typed(make_client):
    def refuse(url, **kw):
        raise ConnectionRefusedError("no route")

    client = HAClient(make_settings(), ws_connect=refuse)
    with pytest.raises(HAConnectionError, match="WebSocket"):
        await client.list_areas()
    await client.aclose()


def test_ws_url_scheme_follows_http_scheme():
    assert make_settings(url="https://ha.example.com").ws_url == "wss://ha.example.com/api/websocket"
    assert make_settings(url="http://10.0.0.5:8123").ws_url == "ws://10.0.0.5:8123/api/websocket"

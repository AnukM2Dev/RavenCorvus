import pytest

from ha_mcp.config import DEFAULT_SENSITIVE_DOMAINS
from ha_mcp.safety import GuardError


async def test_list_entities_domain_and_search_filters(make_ops):
    ops = make_ops()
    lights = await ops.list_entities(domain="light")
    assert {e["entity_id"] for e in lights["entities"]} == {"light.kitchen", "light.bedroom"}
    found = await ops.list_entities(search="attic")
    assert [e["entity_id"] for e in found["entities"]] == ["switch.fan"]


async def test_list_entities_by_area_uses_registries(make_ops, ha):
    out = await make_ops().list_entities(area="Kitchen")
    assert [e["entity_id"] for e in out["entities"]] == ["light.kitchen"]
    assert out["entities"][0]["area"] == "Kitchen"
    assert "config/area_registry/list" in ha.ws_commands


async def test_list_entities_without_area_does_not_open_websocket(make_ops, ha):
    await make_ops().list_entities(domain="light")
    assert ha.ws_commands == []


async def test_list_entities_truncation_flag(make_ops):
    out = await make_ops().list_entities(limit=2)
    assert out["returned"] == 2 and out["total"] == 7 and out["truncated"] is True


async def test_get_state_compact_vs_full(make_ops):
    ops = make_ops()
    assert "attributes" not in await ops.get_state("light.kitchen")
    assert (await ops.get_state("light.kitchen", full=True))["attributes"]["brightness"] == 200


async def test_search_entities_ranks_by_token_match(make_ops):
    out = await make_ops().search_entities("kitchen light")
    assert out["matches"][0]["entity_id"] == "light.kitchen"
    assert (await make_ops().search_entities("   "))["matches"] == []


async def test_list_devices_filters_by_area(make_ops):
    ops = make_ops()
    assert len(await ops.list_devices(area="kitchen")) == 1
    assert await ops.list_devices(area="bedroom") == []


async def test_history_and_logbook_are_compacted(make_ops):
    ops = make_ops()
    hist = await ops.get_history(["light.kitchen"], hours=1)
    assert hist[0]["entity_id"] == "light.kitchen" and hist[0]["count"] == 2
    log = await ops.get_logbook(entity_id="light.kitchen")
    assert log[0]["message"] == "turned on"


async def test_error_log_is_tailed(make_ops):
    out = await make_ops().get_error_log(lines=2)
    assert out == "line 498\nline 499"


# ------------------------------------------------------------------ writes
async def test_call_service_returns_what_actually_changed(make_ops, ha):
    out = await make_ops().call_service("light", "turn_off", {"entity_id": "light.kitchen"})
    assert out["called"] == "light.turn_off"
    assert out["changed"] == [
        {"entity_id": "light.kitchen", "from": "on", "to": "off", "attrs": {"brightness": 200}}
    ]
    assert "note" not in out
    assert ha.states["light.kitchen"]["state"] == "off"


async def test_call_service_with_no_change_says_to_verify(make_ops):
    out = await make_ops().call_service("light", "turn_off", {"entity_id": "light.bedroom"})
    assert out["changed"] == []
    assert "get_state" in out["note"]


async def test_call_service_multiple_entities(make_ops):
    out = await make_ops().call_service(
        "light", "turn_on", {"entity_id": ["light.kitchen", "light.bedroom"]}
    )
    assert [c["entity_id"] for c in out["changed"]] == ["light.bedroom"]  # kitchen already on


async def test_blocked_call_raises_and_sends_nothing(make_ops, ha):
    ops = make_ops()
    with pytest.raises(GuardError, match="HASS_ALLOW_SENSITIVE_DOMAINS"):
        await ops.call_service("lock", "unlock", {"entity_id": "lock.front_door"})
    assert not any(r.method == "POST" for r in ha.requests)
    assert ha.states["lock.front_door"]["state"] == "locked"


async def test_sensitive_call_works_after_opt_in(make_ops, ha):
    ops = make_ops(allow_sensitive=frozenset({"lock"}))
    out = await ops.call_service("lock", "unlock", {"entity_id": "lock.front_door"})
    assert out["changed"][0]["to"] == "unlocked"


async def test_read_only_blocks_writes(make_ops, ha):
    ops = make_ops(read_only=True)
    with pytest.raises(GuardError, match="HASS_READ_ONLY"):
        await ops.call_service("light", "turn_off", {"entity_id": "light.kitchen"})
    with pytest.raises(GuardError):
        await ops.set_state("input_boolean.x", "on")
    with pytest.raises(GuardError):
        await ops.fire_event("e")
    assert not any(r.method == "POST" for r in ha.requests)


async def test_dry_run_changes_nothing_and_reports_policy(make_ops, ha):
    ops = make_ops()
    out = await ops.call_service("light", "turn_off", {"entity_id": "light.kitchen"}, dry_run=True)
    assert out["dry_run"] and out["allowed"] and out["service_exists"]
    assert out["current_states"][0]["state"] == "on"
    assert ha.states["light.kitchen"]["state"] == "on"
    assert not any(r.method == "POST" for r in ha.requests)


async def test_dry_run_of_blocked_call_returns_reason_instead_of_raising(make_ops):
    out = await make_ops().call_service("lock", "unlock", {"entity_id": "lock.front_door"}, dry_run=True)
    assert out["allowed"] is False
    assert "HASS_ALLOW_SENSITIVE_DOMAINS" in out["reason"]


async def test_dry_run_flags_unknown_service(make_ops):
    out = await make_ops().call_service("light", "explode", {"entity_id": "light.kitchen"}, dry_run=True)
    assert out["allowed"] and out["service_exists"] is False


async def test_set_state_writes_and_reports(make_ops, ha):
    out = await make_ops().set_state("input_boolean.guest", "on", attributes={"friendly_name": "Guest"})
    assert out["changed"][0]["to"] == "on" and out["changed"][0]["from"] is None
    assert "does not command the device" in out["note"]
    assert ha.states["input_boolean.guest"]["state"] == "on"


async def test_set_state_on_sensitive_domain_blocked(make_ops, ha):
    with pytest.raises(GuardError):
        await make_ops().set_state("lock.front_door", "unlocked")
    assert ha.states["lock.front_door"]["state"] == "locked"


async def test_fire_event_requires_opt_in(make_ops):
    with pytest.raises(GuardError):
        await make_ops().fire_event("custom_event")
    ok = await make_ops(allow_sensitive=DEFAULT_SENSITIVE_DOMAINS).fire_event("custom_event", data={"a": 1})
    assert ok["event_type"] == "custom_event"

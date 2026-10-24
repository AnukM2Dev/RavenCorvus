import json

import pytest

from ha_mcp.server import ToolError, build_server

READ_TOOLS = {
    "ha_status",
    "list_entities",
    "get_state",
    "search_entities",
    "list_services",
    "list_areas",
    "list_devices",
    "get_history",
    "get_logbook",
    "render_template",
    "get_error_log",
    "check_config",
    "get_automation_config",
    "list_automation_traces",
    "get_automation_trace",
}
WRITE_TOOLS = {"call_service", "set_state", "fire_event", "create_backup"}


def payload(result):
    """Normalize call_tool output across mcp 1.x (content list / tuple) and 2.x (CallToolResult)."""
    if hasattr(result, "content"):
        content = result.content
    else:
        content = result[0] if isinstance(result, tuple) else result
    text = "".join(getattr(c, "text", "") for c in content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def tool_names(server) -> set[str]:
    return {t.name for t in await server.list_tools()}


async def test_full_surface_registered(make_ops):
    names = await tool_names(build_server(make_ops()))
    assert names == READ_TOOLS | WRITE_TOOLS


async def test_read_only_removes_write_tools_entirely(make_ops):
    names = await tool_names(build_server(make_ops(read_only=True)))
    assert names == READ_TOOLS
    assert not names & WRITE_TOOLS


def schema(tool):
    # mcp 2.x: input_schema; mcp 1.x: inputSchema
    return getattr(tool, "input_schema", None) or tool.inputSchema


async def test_tools_have_descriptions_and_schemas(make_ops):
    for t in await build_server(make_ops()).list_tools():
        assert t.description and schema(t)["type"] == "object", t.name


async def test_call_service_schema_exposes_dry_run(make_ops):
    tools = {t.name: t for t in await build_server(make_ops()).list_tools()}
    s = schema(tools["call_service"])
    assert {"domain", "service", "data", "dry_run", "return_response"} <= set(s["properties"])
    assert set(s["required"]) == {"domain", "service"}


async def test_read_tool_roundtrip(make_ops):
    server = build_server(make_ops())
    out = payload(await server.call_tool("get_state", {"entity_id": "light.kitchen"}))
    assert out["state"] == "on" and out["name"] == "Kitchen Light"


async def test_write_tool_roundtrip_reports_change(make_ops):
    server = build_server(make_ops())
    out = payload(
        await server.call_tool(
            "call_service",
            {"domain": "light", "service": "turn_off", "data": {"entity_id": "light.kitchen"}},
        )
    )
    assert out["changed"][0]["from"] == "on" and out["changed"][0]["to"] == "off"


async def test_blocked_write_surfaces_as_tool_error_naming_setting(make_ops, ha):
    server = build_server(make_ops())
    with pytest.raises(ToolError, match=r"BLOCKED.*HASS_ALLOW_SENSITIVE_DOMAINS"):
        await server.call_tool(
            "call_service",
            {"domain": "lock", "service": "unlock", "data": {"entity_id": "lock.front_door"}},
        )
    assert ha.states["lock.front_door"]["state"] == "locked"


async def test_ha_errors_surface_as_clean_tool_errors(make_ops):
    server = build_server(make_ops())
    with pytest.raises(ToolError, match="404"):
        await server.call_tool("get_state", {"entity_id": "light.nope"})


async def test_dry_run_through_server(make_ops):
    server = build_server(make_ops())
    out = payload(
        await server.call_tool(
            "call_service",
            {
                "domain": "lock",
                "service": "unlock",
                "data": {"entity_id": "lock.front_door"},
                "dry_run": True,
            },
        )
    )
    assert out["dry_run"] and out["allowed"] is False

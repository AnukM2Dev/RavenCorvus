"""MCP server. Run with ``ha-mcp`` (stdio) or ``ha-mcp --transport http`` (remote).

Write tools are not registered at all when HASS_READ_ONLY is set, so an agent
never sees an action it cannot take.
"""

from __future__ import annotations

import sys
from typing import Any

# mcp 2.x renamed FastMCP to MCPServer; mcp 1.x still ships FastMCP. Support both.
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - exercised only on mcp<2
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

from .client import HAError
from .config import ConfigError, Settings
from .ops import Operations
from .safety import GuardError

INSTRUCTIONS = """\
Tools for reading and controlling a Home Assistant instance.

- Resolve names first: use search_entities, list_areas or list_entities (filter by area/domain)
  to find the exact entity_id before acting.
- Prefer call_service with dry_run=true when unsure; it reports whether the call is allowed and the
  current state of each target without changing anything.
- Write results list the states that actually changed. If `changed` is empty, verify with get_state
  rather than assuming the action landed.
- Some domains (lock, alarm_control_panel, cover, climate, valve) are blocked unless the operator
  enabled them. A blocked call returns an error naming the setting; do not try to work around it.
"""


def _wrap(fn):  # noqa: ANN001 - small decorator
    """Turn HA/guard errors into ToolError so the agent sees the real message.

    This must be an explicit ToolError: on mcp 2.x any other exception is reported
    to the client as a bare "Error executing tool", which would hide the
    "set HASS_... to allow this" guidance in a blocked-write message.
    """

    async def inner(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except GuardError as exc:
            raise ToolError(f"BLOCKED: {exc}") from None
        except HAError as exc:
            raise ToolError(str(exc)) from None

    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
    inner.__annotations__ = fn.__annotations__
    inner.__wrapped__ = fn  # preserve signature for FastMCP introspection
    return inner


def build_server(ops: Operations | None = None, settings: Settings | None = None) -> FastMCP:
    if ops is None:
        settings = settings or Settings.from_env()
        ops = Operations(settings)
    settings = ops.settings
    mcp = FastMCP("homeassistant", instructions=INSTRUCTIONS)

    def tool(fn):  # noqa: ANN001
        return mcp.tool()(_wrap(fn))

    # ------------------------------------------------------------------ read
    @tool
    async def ha_status() -> dict:
        """Check connectivity and return Home Assistant version, location, and whether writes are enabled."""
        return await ops.ha_status()

    @tool
    async def list_entities(
        domain: str | None = None,
        area: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> dict:
        """List entities with compact state. Filter by domain (e.g. 'light'), area name/id
        (e.g. 'kitchen'), or a substring of the entity_id/friendly name. Results are capped by
        `limit`; `truncated` says whether more exist. Use get_state(full=true) for attributes."""
        return await ops.list_entities(domain=domain, area=area, search=search, limit=limit)

    @tool
    async def get_state(entity_id: str, full: bool = False) -> dict:
        """Get one entity's state. Set full=true for every attribute and timestamps."""
        return await ops.get_state(entity_id, full=full)

    @tool
    async def search_entities(query: str, limit: int = 25) -> dict:
        """Find entities by free-text name, e.g. 'kitchen ceiling light'. Ranked by token match
        against entity_id and friendly name."""
        return await ops.search_entities(query, limit=limit)

    @tool
    async def list_services(domain: str | None = None) -> list:
        """List callable services (domain.service) with descriptions and field names.
        Pass a domain such as 'light' to narrow the list."""
        return await ops.list_services(domain=domain)

    @tool
    async def list_areas() -> list:
        """List areas (rooms/zones) from the area registry."""
        return await ops.list_areas()

    @tool
    async def list_devices(area: str | None = None) -> list:
        """List devices from the device registry, optionally filtered to one area name/id."""
        return await ops.list_devices(area=area)

    @tool
    async def get_history(
        entity_ids: list[str],
        hours: float = 24,
        start: str | None = None,
        end: str | None = None,
    ) -> list:
        """State history for one or more entities. Defaults to the last `hours`; or pass ISO-8601
        `start` / `end` timestamps. Returns [timestamp, state] change pairs per entity."""
        return await ops.get_history(entity_ids, hours=hours, start=start, end=end)

    @tool
    async def get_logbook(
        entity_id: str | None = None,
        hours: float = 24,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
    ) -> list:
        """Logbook entries (who/what changed what, and when), optionally for one entity."""
        return await ops.get_logbook(
            entity_id=entity_id, hours=hours, start=start, end=end, limit=limit
        )

    @tool
    async def render_template(template: str) -> str:
        """Render a Jinja2 template on the Home Assistant server, e.g.
        "{{ states('sensor.outdoor_temperature') }}" or "{{ area_entities('kitchen') }}"."""
        return await ops.render_template(template)

    @tool
    async def get_error_log(lines: int = 100) -> str:
        """Tail of Home Assistant's error log (last `lines` lines)."""
        return await ops.get_error_log(lines=lines)

    # ------------------------------------------------------------------ write
    if not settings.read_only:

        @tool
        async def call_service(
            domain: str,
            service: str,
            data: dict | None = None,
            dry_run: bool = False,
            return_response: bool = False,
        ) -> dict:
            """Call a Home Assistant service, e.g. domain='light', service='turn_on',
            data={'entity_id': 'light.kitchen', 'brightness': 128}. With dry_run=true nothing is
            sent: you get whether the call is allowed, whether the service exists, and the targets'
            current states. A real call returns the entity states that actually changed
            (from -> to). Set return_response=true for services that return data."""
            return await ops.call_service(
                domain, service, data, dry_run=dry_run, return_response=return_response
            )

        @tool
        async def set_state(entity_id: str, state: str, attributes: dict | None = None) -> dict:
            """Write a state into Home Assistant's state machine (virtual/helper entities). This does
            NOT command a physical device; use call_service for that."""
            return await ops.set_state(entity_id, state, attributes=attributes)

        @tool
        async def fire_event(event_type: str, data: dict | None = None) -> dict:
            """Fire an event on the Home Assistant event bus (can trigger automations)."""
            return await ops.fire_event(event_type, data=data)

    return mcp


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="ha-mcp", description="RavenCorvus Home Assistant MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("RAVEN_TRANSPORT", "stdio"),
        help="stdio (default, local clients) or http (remote, needs RAVEN_API_KEY)",
    )
    parser.add_argument("--host", help="http bind address (env RAVEN_HOST, default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="http port (env RAVEN_PORT, default 8765)")
    args = parser.parse_args(argv)

    try:
        server = build_server()
        if args.transport == "http":
            from dataclasses import replace

            from .http_transport import HttpSettings, serve

            http = HttpSettings.from_env()
            if args.host:
                http = replace(http, host=args.host)
            if args.port:
                http = replace(http, port=args.port)
    except ConfigError as exc:
        print(f"ha-mcp configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.transport == "http":
        print(f"ha-mcp: serving streamable HTTP on {http.host}:{http.port}{http.path}", file=sys.stderr)
        serve(server, http)
    else:
        server.run(transport="stdio")

if __name__ == "__main__":
    main()

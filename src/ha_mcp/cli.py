"""`ha` command: the same operations as the MCP server, for a bare shell.

    ha status
    ha entities --domain light --area kitchen
    ha call light turn_on -e light.kitchen -d '{"brightness": 128}' --dry-run

Output is a table for listings and JSON otherwise; ``-f json`` forces JSON
everywhere (handy for piping to jq). Exit codes: 0 ok, 1 Home Assistant error,
2 blocked by policy or bad configuration/arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable, Sequence

from .client import HAError
from .config import ConfigError, Settings
from .formatting import render_table
from .ops import Operations
from .safety import GuardError


# --------------------------------------------------------------------------- output
def _emit(data: Any, fmt: str, columns: list[str] | None = None, rows_key: str | None = None) -> None:
    if fmt == "table" and columns:
        rows = data[rows_key] if rows_key else data
        flat = [_flatten(r) for r in rows]
        print(render_table(flat, columns))
        if rows_key and isinstance(data, dict) and data.get("truncated"):
            print(f"\n(showing {data['returned']} of {data['total']}; raise --limit for more)")
        return
    if isinstance(data, str):
        print(data)
        return
    print(json.dumps(data, indent=2, default=str))


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    attrs = out.pop("attrs", None)
    if attrs and "unit_of_measurement" in attrs and out.get("state") is not None:
        out["state"] = f"{out['state']} {attrs['unit_of_measurement']}"
    return out


def _json_arg(raw: str | None, what: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return value


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ha", description="Home Assistant from the shell.")
    p.add_argument("-f", "--format", choices=["table", "json"], default="table")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="connectivity + version")

    s = sub.add_parser("entities", help="list entities")
    s.add_argument("--domain")
    s.add_argument("--area")
    s.add_argument("--search")
    s.add_argument("--limit", type=int, default=100)

    s = sub.add_parser("state", help="one entity's state")
    s.add_argument("entity_id")
    s.add_argument("--full", action="store_true", help="include all attributes")

    s = sub.add_parser("search", help="free-text entity search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=25)

    s = sub.add_parser("services", help="list services")
    s.add_argument("--domain")

    sub.add_parser("areas", help="list areas")

    s = sub.add_parser("devices", help="list devices")
    s.add_argument("--area")

    s = sub.add_parser("history", help="state history")
    s.add_argument("entity_ids", nargs="+")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--start")
    s.add_argument("--end")

    s = sub.add_parser("logbook", help="logbook entries")
    s.add_argument("--entity")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--limit", type=int, default=100)

    s = sub.add_parser("template", help="render a Jinja2 template")
    s.add_argument("template", help="template text, or '-' to read stdin")

    s = sub.add_parser("error-log", help="tail the error log")
    s.add_argument("--lines", type=int, default=100)

    sub.add_parser("check-config", help="validate HA configuration")

    s = sub.add_parser("automation", help="an automation's stored config")
    s.add_argument("entity_id")

    s = sub.add_parser("traces", help="recent runs of an automation")
    s.add_argument("entity_id")
    s.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("trace", help="step-by-step trace of one automation run")
    s.add_argument("entity_id")
    s.add_argument("--run-id", help="defaults to the latest run")
    s.add_argument("--max-steps", type=int, default=60)

    sub.add_parser("backup", help="start a Home Assistant backup")

    s = sub.add_parser("call", help="call a service")
    s.add_argument("domain")
    s.add_argument("service")
    s.add_argument("-e", "--entity", action="append", default=[], help="entity_id (repeatable)")
    s.add_argument("-d", "--data", help="service data as a JSON object")
    s.add_argument("--dry-run", action="store_true", help="check policy and targets; send nothing")
    s.add_argument("--return-response", action="store_true")

    s = sub.add_parser("set-state", help="write a state into the state machine")
    s.add_argument("entity_id")
    s.add_argument("state")
    s.add_argument("-a", "--attributes", help="attributes as a JSON object")

    s = sub.add_parser("fire-event", help="fire an event")
    s.add_argument("event_type")
    s.add_argument("-d", "--data", help="event data as a JSON object")
    return p


# --------------------------------------------------------------------------- dispatch
async def run(args: argparse.Namespace, ops: Operations) -> None:
    fmt = args.format
    cmd = args.cmd

    if cmd == "status":
        _emit(await ops.ha_status(), fmt)
    elif cmd == "entities":
        data = await ops.list_entities(
            domain=args.domain, area=args.area, search=args.search, limit=args.limit
        )
        _emit(data, fmt, ["entity_id", "state", "name", "area"], "entities")
    elif cmd == "state":
        _emit(await ops.get_state(args.entity_id, full=args.full), fmt)
    elif cmd == "search":
        data = await ops.search_entities(args.query, limit=args.limit)
        _emit(data["matches"], fmt, ["entity_id", "state", "name"])
    elif cmd == "services":
        _emit(await ops.list_services(domain=args.domain), fmt, ["service", "description"])
    elif cmd == "areas":
        _emit(await ops.list_areas(), fmt, ["area_id", "name", "floor_id"])
    elif cmd == "devices":
        _emit(
            await ops.list_devices(area=args.area), fmt, ["name", "manufacturer", "model", "area"]
        )
    elif cmd == "history":
        _emit(
            await ops.get_history(args.entity_ids, hours=args.hours, start=args.start, end=args.end),
            "json",
        )
    elif cmd == "logbook":
        data = await ops.get_logbook(
            entity_id=args.entity, hours=args.hours, start=args.start, end=args.end, limit=args.limit
        )
        _emit(data, fmt, ["when", "name", "message"])
    elif cmd == "template":
        text = sys.stdin.read() if args.template == "-" else args.template
        _emit(await ops.render_template(text), fmt)
    elif cmd == "error-log":
        _emit(await ops.get_error_log(lines=args.lines), fmt)
    elif cmd == "check-config":
        _emit(await ops.check_config(), "json")
    elif cmd == "automation":
        _emit(await ops.get_automation_config(args.entity_id), "json")
    elif cmd == "traces":
        data = await ops.list_automation_traces(args.entity_id, limit=args.limit)
        _emit(data, fmt, ["run_id", "start", "result", "last_step"], "runs")
    elif cmd == "trace":
        _emit(
            await ops.get_automation_trace(
                args.entity_id, run_id=args.run_id, max_steps=args.max_steps
            ),
            "json",
        )
    elif cmd == "backup":
        _emit(await ops.create_backup(), "json")
    elif cmd == "call":
        data = _json_arg(args.data, "--data")
        if args.entity:
            data["entity_id"] = args.entity if len(args.entity) > 1 else args.entity[0]
        _emit(
            await ops.call_service(
                args.domain,
                args.service,
                data,
                dry_run=args.dry_run,
                return_response=args.return_response,
            ),
            "json",
        )
    elif cmd == "set-state":
        _emit(
            await ops.set_state(
                args.entity_id, args.state, attributes=_json_arg(args.attributes, "--attributes")
            ),
            "json",
        )
    elif cmd == "fire-event":
        _emit(await ops.fire_event(args.event_type, data=_json_arg(args.data, "--data")), "json")
    else:  # pragma: no cover - argparse enforces choices
        raise ValueError(f"unknown command {cmd}")


async def _main(argv: Sequence[str], ops_factory: Callable[[], Operations] | None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ops = ops_factory() if ops_factory else Operations(Settings.from_env())
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        await run(args, ops)
        return 0
    except GuardError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except HAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await ops.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    sys.exit(asyncio.run(_main(list(sys.argv[1:] if argv is None else argv), None)))


if __name__ == "__main__":
    main()

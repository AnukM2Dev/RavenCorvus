"""House watch: a scheduled, read-only Claude agent for Home Assistant.

Two jobs, both driven by the Anthropic Messages API with RavenCorvus' read
tools called in-process:

* watch   - look over the house and push a notification only if something needs
            attention. Recent alerts and a "what's normal here" note are given to
            the model so it does not repeat itself or flag routine behaviour.
* nightly - review the last day and rewrite the short baseline note.

The agent can only call read operations. The one write is the final
notification, restricted to the ``notify`` domain and performed by this module,
not by the model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import ConfigError, Settings
from .ops import Operations

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOOL_CHARS = 12000
MAX_NOTE_CHARS = 4000

WATCH_SYSTEM = """\
You are the house watch for a farmhouse running Home Assistant. You can only read; you cannot control anything.
Look for things a person would want to know about now: doors, locks or garage left open, devices or
integrations offline or unavailable for a long time, low batteries (especially locks and sensors), water or
smoke or freeze risk, cameras/Frigate problems, unusual temperatures, repeated errors in the HA log.
Do not report routine states or things the baseline note says are normal. Do not repeat an alert that was
already sent recently unless it got worse. Be economical: query only what you need, a handful of tool calls.
Finish with one JSON object and nothing after it:
{"notify": true|false, "title": "short", "message": "1-4 plain sentences naming the entities and what to do"}
Use notify=false when nothing needs attention."""

NIGHTLY_SYSTEM = """\
You maintain a short "what's normal here" note for a farmhouse's Home Assistant, used to avoid false alarms.
Review the previous 24 hours (logbook, error log, key states) and the existing note. Output ONLY the new note,
at most 300 words, plain text bullets: usual occupancy/usage rhythms, devices that are routinely unavailable or
noisy, typical temperature ranges, known quirks, anything intentionally left open or off. Keep durable facts from
the old note, drop stale ones, and never include secrets or tokens."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_entities",
        "description": "List entities, optionally filtered by domain (e.g. lock, binary_sensor, sensor), area or text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "area": {"type": "string"},
                "search": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_state",
        "description": "Current state of one entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "search_entities",
        "description": "Find entities by name words.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_areas",
        "description": "List areas.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_history",
        "description": "State history for entities over the last N hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_ids": {"type": "array", "items": {"type": "string"}},
                "hours": {"type": "number"},
            },
            "required": ["entity_ids"],
        },
    },
    {
        "name": "get_logbook",
        "description": "Logbook events over the last N hours, optionally for one entity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "number"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_error_log",
        "description": "Tail of the Home Assistant error log.",
        "input_schema": {"type": "object", "properties": {"lines": {"type": "integer"}}},
    },
]


# ----------------------------------------------------------------------- config
def _times(raw: str, name: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", part)
        if not m:
            raise ConfigError(f"{name} must be comma-separated HH:MM times, got {part!r}")
        out.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(out))


@dataclass(frozen=True)
class WatchConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    notify_service: str = ""  # e.g. notify.mobile_app_pixel; empty = log only
    watch_times: list[tuple[int, int]] = field(default_factory=lambda: [(8, 0), (18, 0)])
    nightly_time: tuple[int, int] = (3, 0)
    data_dir: Path = Path("/data")
    max_turns: int = 12

    @classmethod
    def from_env(cls) -> "WatchConfig":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ConfigError("ANTHROPIC_API_KEY is not set")
        notify = os.environ.get("WATCH_NOTIFY_SERVICE", "").strip()
        if notify and not re.fullmatch(r"notify\.[a-z0-9_]+", notify):
            raise ConfigError("WATCH_NOTIFY_SERVICE must look like notify.mobile_app_yourphone")
        times = _times(os.environ.get("WATCH_TIMES", "08:00,18:00"), "WATCH_TIMES") or [(8, 0)]
        nightly = _times(os.environ.get("WATCH_NIGHTLY_TIME", "03:00"), "WATCH_NIGHTLY_TIME")
        return cls(
            api_key=key,
            model=os.environ.get("WATCH_MODEL", "").strip() or DEFAULT_MODEL,
            notify_service=notify,
            watch_times=times,
            nightly_time=nightly[0] if nightly else (3, 0),
            data_dir=Path(os.environ.get("WATCH_DATA_DIR", "/data")),
        )

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "house_memory.md"

    @property
    def alerts_path(self) -> Path:
        return self.data_dir / "recent_alerts.json"


# ------------------------------------------------------------------------ tools
async def run_tool(ops: Operations, name: str, args: dict[str, Any]) -> str:
    """Execute one allowed read tool and return text for the model."""
    try:
        if name == "list_entities":
            res: Any = await ops.list_entities(
                domain=args.get("domain"),
                area=args.get("area"),
                search=args.get("search"),
                limit=int(args.get("limit") or 100),
            )
        elif name == "get_state":
            res = await ops.get_state(str(args["entity_id"]))
        elif name == "search_entities":
            res = await ops.search_entities(str(args["query"]), limit=int(args.get("limit") or 25))
        elif name == "list_areas":
            res = await ops.list_areas()
        elif name == "get_history":
            res = await ops.get_history(list(args["entity_ids"]), hours=float(args.get("hours") or 24))
        elif name == "get_logbook":
            res = await ops.get_logbook(
                entity_id=args.get("entity_id"),
                hours=float(args.get("hours") or 24),
                limit=int(args.get("limit") or 100),
            )
        elif name == "get_error_log":
            res = await ops.get_error_log(lines=int(args.get("lines") or 100))
        else:
            return f"Unknown tool {name!r}"
    except Exception as exc:  # tool errors go back to the model, not up the stack
        return f"Error: {exc}"
    text = res if isinstance(res, str) else json.dumps(res, default=str)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + "\n...[truncated; narrow your query]"
    return text


# ------------------------------------------------------------------------ agent
async def run_agent(
    cfg: WatchConfig,
    ops: Operations,
    system: str,
    user: str,
    *,
    http: httpx.AsyncClient,
) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    for _ in range(cfg.max_turns):
        resp = await http.post(
            API_URL,
            headers={
                "x-api-key": cfg.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": cfg.model,
                "max_tokens": 1500,
                "system": system,
                "tools": TOOLS,
                "messages": messages,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        content = data.get("content", [])
        messages.append({"role": "assistant", "content": content})
        calls = [b for b in content if b.get("type") == "tool_use"]
        if not calls:
            return "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        results = []
        for call in calls:
            out = await run_tool(ops, call["name"], call.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
        messages.append({"role": "user", "content": results})
    # Out of turns: ask for a final answer without tools.
    messages.append({"role": "user", "content": "Out of tool budget. Give your final answer now."})
    resp = await http.post(
        API_URL,
        headers={"x-api-key": cfg.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"},
        json={"model": cfg.model, "max_tokens": 1000, "system": system, "messages": _text_only(messages)},
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:300]}")
    return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text").strip()


def _text_only(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten a tool conversation to plain text so it can be sent without a tools block."""
    parts: list[str] = []
    for m in messages:
        if isinstance(m["content"], str):
            parts.append(f"[{m['role']}] {m['content']}")
            continue
        for b in m["content"]:
            if b.get("type") == "text":
                parts.append(f"[{m['role']}] {b['text']}")
            elif b.get("type") == "tool_use":
                parts.append(f"[tool call] {b['name']} {json.dumps(b.get('input'))}")
            elif b.get("type") == "tool_result":
                parts.append(f"[tool result] {str(b.get('content'))[:3000]}")
    return [{"role": "user", "content": "\n".join(parts)}]


def parse_verdict(text: str) -> dict[str, Any]:
    """Pull the last JSON object out of the model's reply."""
    for m in reversed(list(re.finditer(r"\{.*?\}", text, re.S))):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "notify" in obj:
            return {
                "notify": bool(obj.get("notify")),
                "title": str(obj.get("title") or "House watch")[:80],
                "message": str(obj.get("message") or "")[:600],
            }
    return {"notify": False, "title": "", "message": ""}


# ------------------------------------------------------------------------- jobs
def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text()
    except OSError:
        return default


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _recent_alerts(cfg: WatchConfig, now: datetime) -> list[dict[str, str]]:
    try:
        alerts = json.loads(_read(cfg.alerts_path, "[]"))
    except json.JSONDecodeError:
        return []
    cutoff = (now - timedelta(hours=48)).isoformat()
    return [a for a in alerts if isinstance(a, dict) and a.get("at", "") >= cutoff][-10:]


async def send_notification(ops: Operations, cfg: WatchConfig, title: str, message: str) -> bool:
    if not cfg.notify_service:
        print(f"[watch] (no notify service configured) {title}: {message}", flush=True)
        return False
    domain, service = cfg.notify_service.split(".", 1)
    await ops.client.call_service(domain, service, {"title": title, "message": message})
    return True


async def run_watch(cfg: WatchConfig, ops: Operations, http: httpx.AsyncClient, now: datetime) -> dict[str, Any]:
    note = _read(cfg.memory_path, "(no baseline yet)")
    recent = _recent_alerts(cfg, now)
    user = (
        f"Current time: {now.strftime('%A %Y-%m-%d %H:%M %Z')}.\n\n"
        f"Baseline note (what's normal here):\n{note[:MAX_NOTE_CHARS]}\n\n"
        f"Alerts already sent in the last 48h:\n{json.dumps(recent) if recent else '(none)'}\n\n"
        "Check the house now."
    )
    verdict = parse_verdict(await run_agent(cfg, ops, WATCH_SYSTEM, user, http=http))
    if verdict["notify"] and verdict["message"]:
        sent = await send_notification(ops, cfg, verdict["title"], verdict["message"])
        recent.append({"at": now.isoformat(), "title": verdict["title"], "message": verdict["message"]})
        _write_atomic(cfg.alerts_path, json.dumps(recent[-20:]))
        verdict["sent"] = sent
    print(f"[watch] {now.isoformat()} notify={verdict['notify']} {verdict['title']}", flush=True)
    return verdict


async def run_nightly(cfg: WatchConfig, ops: Operations, http: httpx.AsyncClient, now: datetime) -> str:
    old = _read(cfg.memory_path, "(none yet)")
    user = (
        f"Current time: {now.strftime('%A %Y-%m-%d %H:%M %Z')}.\n\nExisting note:\n{old[:MAX_NOTE_CHARS]}\n\n"
        "Review the last 24 hours and write the updated note."
    )
    new = (await run_agent(cfg, ops, NIGHTLY_SYSTEM, user, http=http)).strip()
    if new:
        _write_atomic(cfg.memory_path, new[:MAX_NOTE_CHARS] + "\n")
    print(f"[watch] nightly note updated ({len(new)} chars)", flush=True)
    return new


# --------------------------------------------------------------------- scheduler
def next_event(now: datetime, cfg: WatchConfig) -> tuple[datetime, str]:
    """Return the next scheduled run after ``now`` and its kind."""
    candidates: list[tuple[datetime, str]] = []
    for day in (0, 1):
        base = (now + timedelta(days=day)).replace(second=0, microsecond=0)
        for h, m in cfg.watch_times:
            candidates.append((base.replace(hour=h, minute=m), "watch"))
        h, m = cfg.nightly_time
        candidates.append((base.replace(hour=h, minute=m), "nightly"))
    return min((c for c in candidates if c[0] > now), key=lambda c: c[0])


async def daemon(cfg: WatchConfig, ops: Operations) -> None:
    status = await ops.ha_status()
    tz = ZoneInfo(status.get("time_zone") or "UTC")
    print(
        f"[watch] started tz={tz} model={cfg.model} watch={cfg.watch_times} nightly={cfg.nightly_time} "
        f"notify={cfg.notify_service or 'log-only'}",
        flush=True,
    )
    async with httpx.AsyncClient(timeout=60) as http:
        while True:
            now = datetime.now(tz)
            when, kind = next_event(now, cfg)
            await asyncio.sleep(max(1.0, (when - now).total_seconds()))
            run_at = datetime.now(tz)
            try:
                if kind == "watch":
                    await run_watch(cfg, ops, http, run_at)
                else:
                    await run_nightly(cfg, ops, http, run_at)
            except Exception as exc:  # keep the daemon alive; log and wait for the next slot
                print(f"[watch] {kind} failed: {exc}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ha-watch", description="Scheduled read-only Claude house watch")
    parser.add_argument("--once", choices=["watch", "nightly"], help="run one job now and exit")
    args = parser.parse_args(argv)
    try:
        cfg = WatchConfig.from_env()
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"ha-watch configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    async def _go() -> None:
        ops = Operations(settings)
        try:
            if args.once:
                tz = ZoneInfo((await ops.ha_status()).get("time_zone") or "UTC")
                async with httpx.AsyncClient(timeout=60) as http:
                    now = datetime.now(tz)
                    if args.once == "watch":
                        print(json.dumps(await run_watch(cfg, ops, http, now), indent=2))
                    else:
                        print(await run_nightly(cfg, ops, http, now))
            else:
                await daemon(cfg, ops)
        finally:
            await ops.aclose()

    asyncio.run(_go())


if __name__ == "__main__":
    main()

"""Operations shared by the MCP server and the CLI.

Each method returns plain JSON-serializable data. Writes go through the
WriteGuard first; blocked calls raise GuardError with a message that names the
setting that would permit them.
"""

from __future__ import annotations

import re
from typing import Any

from .client import HABadRequestError, HAError, HAClient, HANotFoundError
from .config import Settings
from .formatting import (
    compact_history,
    compact_logbook,
    compact_service_domains,
    compact_state,
    compact_states,
    summarize_changes,
    tail_text,
)
from .safety import Decision, GuardError, WriteGuard, extract_entity_ids

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Operations:
    def __init__(self, settings: Settings, client: HAClient | None = None) -> None:
        self.settings = settings
        self.client = client or HAClient(settings)
        self.guard = WriteGuard(settings)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ================================================================== read
    async def ha_status(self) -> dict[str, Any]:
        return await self.client.status()

    async def list_entities(
        self,
        *,
        domain: str | None = None,
        area: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        states = await self.client.get_states()
        if domain:
            prefix = domain.lower().rstrip(".") + "."
            states = [s for s in states if s["entity_id"].startswith(prefix)]
        if search:
            needle = search.lower()
            states = [
                s
                for s in states
                if needle in s["entity_id"].lower()
                or needle in (s.get("attributes", {}).get("friendly_name") or "").lower()
            ]

        area_map: dict[str, dict[str, str]] = {}
        if area:
            area_map = await self.client.entity_area_map()
            want = area.lower()
            states = [
                s
                for s in states
                if (info := area_map.get(s["entity_id"]))
                and want in (info["area"].lower(), info["area_id"].lower())
            ]

        total = len(states)
        states = states[: max(1, limit)]
        return {
            "total": total,
            "returned": len(states),
            "truncated": total > len(states),
            "entities": compact_states(states, area_map=area_map or None),
        }

    async def get_state(self, entity_id: str, *, full: bool = False) -> dict[str, Any]:
        return compact_state(await self.client.get_state(entity_id), full=full)

    async def search_entities(self, query: str, *, limit: int = 25) -> dict[str, Any]:
        """Rank entities by how many query tokens appear in their id or name."""
        q = _tokens(query)
        if not q:
            return {"query": query, "matches": []}
        scored: list[tuple[int, dict[str, Any]]] = []
        for s in await self.client.get_states():
            haystack = set(
                _tokens(s["entity_id"]) + _tokens(s.get("attributes", {}).get("friendly_name") or "")
            )
            hits = sum(1 for t in q if t in haystack)
            partial = sum(1 for t in q if t not in haystack and any(t in h for h in haystack))
            score = hits * 2 + partial
            if score:
                scored.append((score, s))
        scored.sort(key=lambda p: (-p[0], p[1]["entity_id"]))
        top = [s for _, s in scored[: max(1, limit)]]
        return {"query": query, "matches": compact_states(top)}

    async def list_services(self, *, domain: str | None = None) -> list[dict[str, Any]]:
        return compact_service_domains(await self.client.get_services(), domain=domain)

    async def list_areas(self) -> list[dict[str, Any]]:
        areas = await self.client.list_areas()
        return [
            {"area_id": a["area_id"], "name": a.get("name"), "floor_id": a.get("floor_id")}
            for a in areas
        ]

    async def list_devices(self, *, area: str | None = None) -> list[dict[str, Any]]:
        reg = await self.client.registries()
        names = {a["area_id"]: a.get("name", a["area_id"]) for a in reg["areas"]}
        rows = [
            {
                "id": d["id"],
                "name": d.get("name_by_user") or d.get("name"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "area_id": d.get("area_id"),
                "area": names.get(d.get("area_id") or ""),
            }
            for d in reg["devices"]
        ]
        if area:
            want = area.lower()
            rows = [
                r for r in rows if want in ((r["area"] or "").lower(), (r["area_id"] or "").lower())
            ]
        return rows

    async def get_history(
        self,
        entity_ids: list[str],
        *,
        hours: float = 24,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        raw = await self.client.get_history(entity_ids, hours=hours, start=start, end=end)
        return compact_history(raw)

    async def get_logbook(
        self,
        *,
        entity_id: str | None = None,
        hours: float = 24,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        raw = await self.client.get_logbook(entity_id=entity_id, hours=hours, start=start, end=end)
        return compact_logbook(raw, limit=limit)

    async def render_template(self, template: str) -> str:
        return await self.client.render_template(template)

    async def get_error_log(self, *, lines: int = 100) -> str:
        return tail_text(await self.client.get_error_log(), lines=lines)

    # ================================================================== diagnostics
    async def check_config(self) -> dict[str, Any]:
        raw = await self.client.check_config()
        return {
            "result": raw.get("result"),
            "errors": raw.get("errors"),
            "warnings": raw.get("warnings"),
        }

    async def _automation_item_id(self, entity_id: str) -> str:
        if not entity_id.startswith("automation."):
            raise HAError(f"{entity_id} is not an automation entity")
        state = await self.client.get_state(entity_id)
        item_id = (state.get("attributes") or {}).get("id")
        if not item_id:
            raise HAError(
                f"{entity_id} has no 'id' attribute. Only automations created in the UI or given "
                "an 'id:' in YAML keep traces."
            )
        return str(item_id)

    async def get_automation_config(self, entity_id: str) -> dict[str, Any]:
        return {"entity_id": entity_id, "config": await self.client.automation_config(entity_id)}

    async def list_automation_traces(self, entity_id: str, *, limit: int = 10) -> dict[str, Any]:
        item_id = await self._automation_item_id(entity_id)
        runs = await self.client.list_traces("automation", item_id)
        runs = sorted(runs, key=lambda r: (r.get("timestamp") or {}).get("start", ""), reverse=True)
        return {
            "entity_id": entity_id,
            "item_id": item_id,
            "runs": [_compact_run(r) for r in runs[: max(1, limit)]],
            "total": len(runs),
        }

    async def get_automation_trace(
        self, entity_id: str, *, run_id: str | None = None, max_steps: int = 60
    ) -> dict[str, Any]:
        item_id = await self._automation_item_id(entity_id)
        if not run_id:
            listing = await self.list_automation_traces(entity_id, limit=1)
            if not listing["runs"]:
                raise HAError(f"No stored traces for {entity_id} yet. Trigger it, then try again.")
            run_id = listing["runs"][0]["run_id"]
        raw = await self.client.get_trace("automation", item_id, run_id)
        return _compact_trace(entity_id, raw, max_steps=max_steps)

    # ================================================================== write
    async def _before_states(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        before: dict[str, dict[str, Any]] = {}
        for eid in entity_ids:
            if "." not in eid:
                continue
            try:
                before[eid] = await self.client.get_state(eid)
            except HANotFoundError:
                continue
        return before

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
        return_response: bool = False,
    ) -> dict[str, Any]:
        decision = self.guard.check_service(domain, service, data)

        if dry_run:
            return await self._dry_run(domain, service, data, decision)
        decision.raise_if_blocked()

        entity_ids = extract_entity_ids(data)
        before = await self._before_states(entity_ids)
        result = await self.client.call_service(
            domain, service, data, return_response=return_response
        )
        changed = result["changed_states"]
        out: dict[str, Any] = {
            "called": f"{domain}.{service}",
            "changed": summarize_changes(before, changed),
        }
        if not changed:
            out["note"] = (
                "Home Assistant reported no state changes. The state may already match, or the "
                "device may report back slowly. Use get_state to confirm."
            )
        if return_response:
            out["service_response"] = result["service_response"]
        return out

    async def _dry_run(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        decision: Decision,
    ) -> dict[str, Any]:
        found = any(
            row["service"] == f"{domain}.{service}"
            for row in compact_service_domains(await self.client.get_services(), domain=domain)
        )
        before = await self._before_states(list(decision.entities))
        return {
            "dry_run": True,
            "would_call": f"{domain}.{service}",
            "data": data or {},
            "allowed": decision.allowed,
            "reason": decision.reason,
            "service_exists": found,
            "current_states": [compact_state(s) for s in before.values()],
        }

    async def set_state(
        self, entity_id: str, state: str, *, attributes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.guard.check_set_state(entity_id).raise_if_blocked()
        before = await self._before_states([entity_id])
        new = await self.client.set_state(entity_id, state, attributes)
        return {
            "entity_id": entity_id,
            "changed": summarize_changes(before, [new]),
            "note": "set_state only edits Home Assistant's state machine; it does not command the device.",
        }

    async def create_backup(self) -> dict[str, Any]:
        """Ask Home Assistant to make a backup using its own backup settings."""
        attempts = [("backup", "create_automatic"), ("hassio", "backup_full")]
        last: Exception | None = None
        for domain, service in attempts:
            self.guard.check_service(domain, service, None).raise_if_blocked()
            try:
                await self.client.call_service(domain, service, None)
            except (HANotFoundError, HABadRequestError) as exc:
                last = exc
                continue
            return {
                "called": f"{domain}.{service}",
                "note": "Backup started. It runs in the background; check Settings > System > Backups.",
            }
        raise HAError(f"No backup service available on this instance ({last})")

    async def fire_event(
        self, event_type: str, *, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.guard.check_fire_event(event_type).raise_if_blocked()
        result = await self.client.fire_event(event_type, data)
        return {"event_type": event_type, "result": result}


def _compact_run(r: dict[str, Any]) -> dict[str, Any]:
    ts = r.get("timestamp") or {}
    out = {
        "run_id": r.get("run_id"),
        "start": ts.get("start"),
        "finish": ts.get("finish"),
        "state": r.get("state"),
        "result": r.get("script_execution"),
        "last_step": r.get("last_step"),
        "trigger": r.get("trigger"),
    }
    if r.get("error"):
        out["error"] = r["error"]
    return {k: v for k, v in out.items() if v is not None}


def _compact_trace(entity_id: str, raw: dict[str, Any], *, max_steps: int) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for path, entries in (raw.get("trace") or {}).items():
        for e in entries or []:
            step: dict[str, Any] = {"path": path, "at": e.get("timestamp")}
            res = e.get("result")
            if isinstance(res, dict):
                for key in ("result", "enabled", "conditions", "params", "response", "running_script"):
                    if key in res:
                        step[key] = res[key]
            elif res is not None:
                step["result"] = res
            if e.get("error"):
                step["error"] = e["error"]
            steps.append(step)
    steps.sort(key=lambda s: s.get("at") or "")
    ctx = raw.get("context") or {}
    ts = raw.get("timestamp") or {}
    out = {
        "entity_id": entity_id,
        "run_id": raw.get("run_id"),
        "start": ts.get("start"),
        "finish": ts.get("finish"),
        "state": raw.get("state"),
        "result": raw.get("script_execution"),
        "trigger": raw.get("trigger"),
        "error": raw.get("error"),
        "context_id": ctx.get("id"),
        "steps": steps[:max_steps],
        "steps_truncated": len(steps) > max_steps,
    }
    return {k: v for k, v in out.items() if v is not None}


__all__ = ["Operations", "GuardError"]

"""Compact Home Assistant payloads so listings do not flood an agent's context.

Full attributes stay available per-entity via ``get_state(..., full=True)``.
"""

from __future__ import annotations

from typing import Any

# Attributes worth surfacing in a compact view when present.
_USEFUL_ATTRS = (
    "unit_of_measurement",
    "device_class",
    "brightness",
    "temperature",
    "current_temperature",
    "hvac_action",
    "hvac_modes",
    "current_position",
    "battery_level",
)

_MAX_VALUE_CHARS = 120


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[: _MAX_VALUE_CHARS - 1] + "…"
    if isinstance(value, (list, tuple)) and len(value) > 8:
        return list(value[:8]) + [f"… +{len(value) - 8} more"]
    return value


def compact_state(
    state: dict[str, Any], *, area: str | None = None, full: bool = False
) -> dict[str, Any]:
    """One-entity summary. ``full=True`` returns the complete attributes."""
    attrs = state.get("attributes", {}) or {}
    out: dict[str, Any] = {
        "entity_id": state["entity_id"],
        "state": state.get("state"),
    }
    name = attrs.get("friendly_name")
    if name:
        out["name"] = name
    if area:
        out["area"] = area
    if full:
        out["attributes"] = attrs
        out["last_changed"] = state.get("last_changed")
        out["last_updated"] = state.get("last_updated")
        return out
    extras = {k: _clip(attrs[k]) for k in _USEFUL_ATTRS if k in attrs}
    if extras:
        out["attrs"] = extras
    return out


def compact_states(
    states: list[dict[str, Any]], *, area_map: dict[str, dict[str, str]] | None = None
) -> list[dict[str, Any]]:
    area_map = area_map or {}
    return [
        compact_state(s, area=area_map.get(s["entity_id"], {}).get("area")) for s in states
    ]


def summarize_changes(
    before: dict[str, dict[str, Any]], changed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Describe what a write actually changed: entity, old state, new state."""
    out: list[dict[str, Any]] = []
    for st in changed:
        eid = st["entity_id"]
        old = before.get(eid)
        out.append(
            {
                "entity_id": eid,
                "from": old.get("state") if old else None,
                "to": st.get("state"),
                "attrs": {
                    k: _clip(v)
                    for k, v in (st.get("attributes") or {}).items()
                    if k in _USEFUL_ATTRS
                },
            }
        )
    return out


def compact_service_domains(
    services: list[dict[str, Any]], *, domain: str | None = None
) -> list[dict[str, Any]]:
    """{domain: {service: {...}}} -> a flat list of {domain, service, description, fields}."""
    rows: list[dict[str, Any]] = []
    for block in services:
        d = block.get("domain")
        if domain and d != domain:
            continue
        for name, spec in (block.get("services") or {}).items():
            rows.append(
                {
                    "service": f"{d}.{name}",
                    "description": _clip((spec.get("description") or "").strip()),
                    "fields": sorted((spec.get("fields") or {}).keys()),
                }
            )
    return rows


def compact_history(series: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Collapse HA's per-entity history into {entity_id, changes: [[ts, state]...]}."""
    out: list[dict[str, Any]] = []
    for entity_series in series:
        if not entity_series:
            continue
        first = entity_series[0]
        eid = first.get("entity_id") or next(
            (p["entity_id"] for p in entity_series if "entity_id" in p), "?"
        )
        changes = [
            [p.get("last_changed") or p.get("last_updated"), p.get("state")] for p in entity_series
        ]
        out.append({"entity_id": eid, "count": len(changes), "changes": changes})
    return out


def compact_logbook(entries: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    rows = [
        {
            "when": e.get("when"),
            "entity_id": e.get("entity_id"),
            "name": e.get("name"),
            "message": e.get("message") or e.get("state"),
        }
        for e in entries[-limit:]
    ]
    return rows


def tail_text(text: str, *, lines: int = 100, max_chars: int = 20000) -> str:
    chunk = "\n".join(text.splitlines()[-lines:])
    if len(chunk) > max_chars:
        chunk = "…" + chunk[-max_chars:]
    return chunk


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """Plain-text table for CLI output."""
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    head = "  ".join(c.upper().ljust(widths[c]) for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    body = ["  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join([head, sep, *body])

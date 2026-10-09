"""Write guard.

Evaluates a proposed write against operator-set policy before it reaches Home
Assistant. Order of checks (first failure wins):

1. HASS_READ_ONLY            - every write is refused
2. HASS_DENIED_ENTITIES      - entity ids / glob patterns that are never touched
3. sensitive domains         - lock, alarm_control_panel, cover, climate, valve
                               need HASS_ALLOW_SENSITIVE_DOMAINS
4. HASS_ALLOWED_DOMAINS      - when set, only these domains may be written

Every refusal names the setting that would permit the call, so the operator
(not the agent) knows exactly what to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Iterable

from .config import Settings

# Keys in a service-call payload that select targets without naming entities.
_BROAD_TARGET_KEYS = ("area_id", "device_id", "floor_id", "label_id")

# Services in these domains fan out to whatever the target resolves to, so the
# guard cannot see which domains will actually be hit.
_FAN_OUT_DOMAINS = frozenset({"homeassistant"})


class GuardError(PermissionError):
    """A write was blocked by policy. The message names the permitting setting."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    entities: tuple[str, ...] = field(default_factory=tuple)
    domains: tuple[str, ...] = field(default_factory=tuple)

    def raise_if_blocked(self) -> None:
        if not self.allowed:
            raise GuardError(self.reason)


def entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0].strip().lower()


def extract_entity_ids(data: dict[str, Any] | None) -> list[str]:
    """Pull entity ids out of a service payload (top level or under ``target``)."""
    if not data:
        return []
    found: list[str] = []
    for container in (data, data.get("target") if isinstance(data.get("target"), dict) else None):
        if not container:
            continue
        raw = container.get("entity_id")
        if raw is None:
            continue
        if isinstance(raw, str):
            found.extend(p.strip() for p in raw.split(",") if p.strip())
        elif isinstance(raw, Iterable):
            found.extend(str(p).strip() for p in raw if str(p).strip())
    return found


def _has_broad_target(data: dict[str, Any] | None) -> bool:
    if not data:
        return False
    for container in (data, data.get("target") if isinstance(data.get("target"), dict) else {}):
        if any(container.get(k) for k in _BROAD_TARGET_KEYS):
            return True
    return False


class WriteGuard:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    # ---------------------------------------------------------------- helpers
    def _denied(self, entity_id: str) -> str | None:
        eid = entity_id.lower()
        for pattern in self.s.denied_entities:
            if fnmatchcase(eid, pattern):
                return pattern
        return None

    def _sensitive_blocked(self, domain: str) -> bool:
        return domain in self.s.sensitive_domains and domain not in self.s.allow_sensitive

    def _block(self, reason: str, **kw: Any) -> Decision:
        return Decision(False, reason, **kw)

    # ---------------------------------------------------------------- service calls
    def check_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> Decision:
        domain = domain.strip().lower()
        entities = tuple(extract_entity_ids(data))
        domains = {domain, *(entity_domain(e) for e in entities if "." in e)}
        ctx = {"entities": entities, "domains": tuple(sorted(domains))}

        if self.s.read_only:
            return self._block(
                "Writes are disabled: HASS_READ_ONLY is set. Unset it to allow service calls.", **ctx
            )

        for eid in entities:
            pattern = self._denied(eid)
            if pattern:
                return self._block(
                    f"Entity '{eid}' is on the denylist (HASS_DENIED_ENTITIES matches '{pattern}'). "
                    "Remove that pattern to allow it.",
                    **ctx,
                )
        # HA also exposes scripts as services (script.<object_id>) with no entity_id in the
        # payload, so match the service name too: a 'script.*' pattern must stop those calls.
        pattern = self._denied(f"{domain}.{service}")
        if pattern:
            return self._block(
                f"Service '{domain}.{service}' is on the denylist (HASS_DENIED_ENTITIES matches "
                f"'{pattern}'). Remove that pattern to allow it.",
                **ctx,
            )

        # A fan-out service or an area/device target could reach sensitive
        # domains without naming them. Treat as sensitive unless fully opted in.
        # (A domain-specific service such as light.turn_off can only reach its
        # own domain, so an area target there is not blind.)
        blind = domain in _FAN_OUT_DOMAINS and (
            _has_broad_target(data) or not entities_all_named(entities)
        )
        if blind and not self.s.sensitive_domains <= self.s.allow_sensitive:
            missing = ", ".join(sorted(self.s.sensitive_domains - self.s.allow_sensitive))
            return self._block(
                f"'{domain}.{service}' targets entities by area/device/label (or generically), so it "
                f"could reach sensitive domains ({missing}). Name explicit entity_ids, or set "
                "HASS_ALLOW_SENSITIVE_DOMAINS to opt in.",
                **ctx,
            )

        for d in sorted(domains):
            if self._sensitive_blocked(d):
                return self._block(
                    f"Domain '{d}' is sensitive and blocked by default. "
                    f"Set HASS_ALLOW_SENSITIVE_DOMAINS={d} (or 'all') to allow it.",
                    **ctx,
                )

        if self.s.allowed_domains:
            for d in sorted(domains):
                if d not in self.s.allowed_domains and d not in _FAN_OUT_DOMAINS:
                    return self._block(
                        f"Domain '{d}' is not in HASS_ALLOWED_DOMAINS "
                        f"({', '.join(sorted(self.s.allowed_domains))}). Add it to allow it.",
                        **ctx,
                    )

        return Decision(True, "allowed", **ctx)

    # ---------------------------------------------------------------- set_state
    def check_set_state(self, entity_id: str) -> Decision:
        """set_state only edits HA's state machine, but a forged lock/alarm
        state misleads automations and people, so it gets the same checks."""
        domain = entity_domain(entity_id)
        return self.check_service(domain, "set_state", {"entity_id": entity_id})

    # ---------------------------------------------------------------- fire_event
    def check_fire_event(self, event_type: str) -> Decision:
        if self.s.read_only:
            return self._block(
                "Writes are disabled: HASS_READ_ONLY is set. Unset it to allow firing events."
            )
        # Events can trigger arbitrary automations, which the guard cannot
        # inspect. Require the same opt-in as sensitive domains.
        if not self.s.sensitive_domains <= self.s.allow_sensitive:
            return self._block(
                f"Firing event '{event_type}' can trigger automations that reach sensitive domains "
                "(lock, alarm, cover, climate, valve). Set HASS_ALLOW_SENSITIVE_DOMAINS=all to allow "
                "events."
            )
        return Decision(True, "allowed")


def entities_all_named(entities: tuple[str, ...]) -> bool:
    """True when the target is a concrete list of entity ids (no 'all'/'none')."""
    return bool(entities) and all("." in e for e in entities)

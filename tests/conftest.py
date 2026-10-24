"""A fake Home Assistant: REST via httpx.MockTransport, registries via a fake WebSocket."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from ha_mcp.client import HAClient
from ha_mcp.config import Settings
from ha_mcp.ops import Operations

TOKEN = "test-token"
URL = "http://ha.test:8123"


def _state(entity_id: str, state: str, **attrs: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attrs,
        "last_changed": "2026-10-09T12:00:00+00:00",
        "last_updated": "2026-10-09T12:00:00+00:00",
    }


# (domain, service) -> state the targeted entities move to
TRANSITIONS = {
    ("light", "turn_on"): "on",
    ("light", "turn_off"): "off",
    ("switch", "turn_on"): "on",
    ("switch", "turn_off"): "off",
    ("lock", "unlock"): "unlocked",
    ("lock", "lock"): "locked",
    ("cover", "open_cover"): "open",
    ("homeassistant", "turn_off"): "off",
}


class FakeHA:
    def __init__(self) -> None:
        self.token = TOKEN
        self.requests: list[httpx.Request] = []
        self.ws_commands: list[str] = []
        self.states: dict[str, dict[str, Any]] = {
            s["entity_id"]: s
            for s in [
                _state("light.kitchen", "on", friendly_name="Kitchen Light", brightness=200),
                _state("light.bedroom", "off", friendly_name="Bedroom Lamp"),
                _state(
                    "sensor.outdoor_temp",
                    "21.5",
                    friendly_name="Outdoor Temperature",
                    unit_of_measurement="°C",
                    device_class="temperature",
                ),
                _state("lock.front_door", "locked", friendly_name="Front Door"),
                _state("cover.garage", "closed", friendly_name="Garage Door"),
                _state("climate.hall", "heat", friendly_name="Hall Thermostat", temperature=20),
                _state("switch.fan", "off", friendly_name="Attic Fan"),
            ]
        }
        self.areas = [
            {"area_id": "kitchen", "name": "Kitchen", "floor_id": None},
            {"area_id": "bedroom", "name": "Bedroom", "floor_id": None},
        ]
        self.devices = [
            {
                "id": "dev1",
                "name": "Hue Bulb",
                "name_by_user": None,
                "area_id": "kitchen",
                "manufacturer": "Signify",
                "model": "LCT015",
            }
        ]
        self.entity_registry = [
            {"entity_id": "light.kitchen", "device_id": "dev1", "area_id": None},
            {"entity_id": "light.bedroom", "device_id": None, "area_id": "bedroom"},
        ]
        self.fail_with: int | None = None  # force every REST call to this status
        self.config_check = {"result": "valid", "errors": None, "warnings": None}
        self.backup_services = {("backup", "create_automatic")}
        self.backups_made: list[str] = []
        self.traces = [
            {
                "run_id": "r1",
                "state": "stopped",
                "script_execution": "finished",
                "last_step": "action/0",
                "trigger": "state of binary_sensor.bedroom_exit",
                "timestamp": {"start": "2026-10-24T02:00:00+00:00", "finish": "2026-10-24T02:00:01+00:00"},
            },
            {
                "run_id": "r2",
                "state": "stopped",
                "script_execution": "failed_conditions",
                "last_step": "condition/0",
                "trigger": "state of binary_sensor.bedroom_exit",
                "timestamp": {"start": "2026-10-24T03:00:00+00:00", "finish": "2026-10-24T03:00:00+00:00"},
            },
        ]

    # ------------------------------------------------------------- REST
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="forced failure")
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="401: Unauthorized")

        path = request.url.path
        method = request.method
        assert path.startswith("/api"), path
        path = path[len("/api") :]

        if path == "/config" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": "2026.10.0",
                    "location_name": "Farmhouse",
                    "state": "RUNNING",
                    "time_zone": "America/New_York",
                    "unit_system": {"temperature": "°F"},
                    "components": ["light", "lock", "sensor"],
                },
            )
        if path == "/states" and method == "GET":
            return httpx.Response(200, json=list(self.states.values()))
        if m := re.fullmatch(r"/states/(.+)", path):
            eid = m.group(1)
            if method == "GET":
                if eid not in self.states:
                    return httpx.Response(404, json={"message": "Entity not found."})
                return httpx.Response(200, json=self.states[eid])
            body = json.loads(request.content)
            self.states[eid] = _state(eid, body["state"], **body.get("attributes", {}))
            return httpx.Response(201, json=self.states[eid])
        if path == "/services" and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "domain": "light",
                        "services": {
                            "turn_on": {"description": "Turn a light on", "fields": {"brightness": {}}},
                            "turn_off": {"description": "Turn a light off", "fields": {}},
                        },
                    },
                    {
                        "domain": "lock",
                        "services": {"unlock": {"description": "Unlock a lock", "fields": {"code": {}}}},
                    },
                ],
            )
        if m := re.fullmatch(r"/services/([^/]+)/([^/]+)", path):
            return self._service(m.group(1), m.group(2), request)
        if path.startswith("/history/period/") and method == "GET":
            ids = request.url.params.get("filter_entity_id", "").split(",")
            return httpx.Response(
                200,
                json=[
                    [
                        {"entity_id": e, "state": "off", "last_changed": "2026-10-09T10:00:00+00:00"},
                        {"state": "on", "last_changed": "2026-10-09T11:00:00+00:00"},
                    ]
                    for e in ids
                ],
            )
        if path.startswith("/logbook/") and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "when": "2026-10-09T11:00:00+00:00",
                        "name": "Kitchen Light",
                        "entity_id": "light.kitchen",
                        "message": "turned on",
                    }
                ],
            )
        if path == "/template" and method == "POST":
            tpl = json.loads(request.content)["template"]
            return httpx.Response(200, text=f"rendered:{tpl}")
        if path == "/config/core/check_config" and method == "POST":
            return httpx.Response(200, json=self.config_check)
        if path == "/error_log" and method == "GET" and getattr(self, "error_log_missing", False):
            return httpx.Response(404, text="404: Not Found")
        if path == "/error_log" and method == "GET":
            return httpx.Response(200, text="\n".join(f"line {i}" for i in range(500)))
        if (m := re.fullmatch(r"/events/(.+)", path)) and method == "POST":
            return httpx.Response(200, json={"message": "Event fired."})
        return httpx.Response(404, text=f"no route {method} {path}")

    def _service(self, domain: str, service: str, request: httpx.Request) -> httpx.Response:
        if domain in ("backup", "hassio"):
            if (domain, service) not in self.backup_services:
                return httpx.Response(400, json={"message": f"Service {domain}.{service} not found."})
            self.backups_made.append(f"{domain}.{service}")
            return httpx.Response(200, json=[])
        target = TRANSITIONS.get((domain, service))
        if target is None:
            return httpx.Response(404, json={"message": "Service not found."})
        body = json.loads(request.content or b"{}")
        raw = body.get("entity_id", [])
        ids = [raw] if isinstance(raw, str) else list(raw)
        changed = []
        for eid in ids:
            st = self.states.get(eid)
            if st and st["state"] != target:
                st["state"] = target
                changed.append(st)
        if "return_response" in request.url.params:
            return httpx.Response(200, json={"changed_states": changed, "service_response": {"ok": True}})
        return httpx.Response(200, json=changed)

    # ------------------------------------------------------------- WebSocket
    def ws_connect(self, url: str, **kwargs: Any) -> "FakeWS":
        self.ws_url = url
        return FakeWS(self)


class FakeWS:
    def __init__(self, ha: FakeHA) -> None:
        self.ha = ha
        self.queue: list[str] = [json.dumps({"type": "auth_required", "ha_version": "2026.10.0"})]

    async def __aenter__(self) -> "FakeWS":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def recv(self) -> str:
        return self.queue.pop(0)

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        if msg["type"] == "auth":
            ok = msg["access_token"] == self.ha.token
            self.queue.append(json.dumps({"type": "auth_ok" if ok else "auth_invalid", "message": "bad token"}))
            return
        self.ha.ws_commands.append(msg["type"])
        # an unrelated event frame first: the client must skip it
        self.queue.append(json.dumps({"id": 999, "type": "event", "event": {}}))
        results = {
            "config/area_registry/list": self.ha.areas,
            "config/device_registry/list": self.ha.devices,
            "config/entity_registry/list": self.ha.entity_registry,
            "automation/config": {"config": {"id": "1700000000001", "alias": "Night Path",
                                             "triggers": [{"trigger": "state"}], "actions": []}},
            "trace/list": self.ha.traces,
            "system_log/list": [
                {"name": "homeassistant.components.tuya", "message": ["Device offline"], "level": "WARNING",
                 "timestamp": 1792850000.0, "count": 3},
                {"name": "custom_components.frigate", "message": ["Camera stream lost"], "level": "ERROR",
                 "timestamp": 1792840000.0, "count": 1},
            ],
            "trace/get": {
                "run_id": msg.get("run_id"),
                "state": "stopped",
                "script_execution": "failed_conditions",
                "trigger": "state of binary_sensor.bedroom_exit",
                "timestamp": {"start": "2026-10-24T03:00:00+00:00"},
                "context": {"id": "ctx1"},
                "trace": {
                    "trigger/0": [{"timestamp": "2026-10-24T03:00:00.001+00:00",
                                   "changed_variables": {"trigger": {}}}],
                    "condition/0": [{"timestamp": "2026-10-24T03:00:00.002+00:00",
                                     "result": {"result": False, "entities": ["sun.sun"]}}],
                },
            },
        }
        if msg["type"] in results:
            self.queue.append(
                json.dumps({"id": msg["id"], "type": "result", "success": True, "result": results[msg["type"]]})
            )
        else:
            self.queue.append(
                json.dumps(
                    {
                        "id": msg["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": "unknown_command", "message": "Unknown command."},
                    }
                )
            )


# --------------------------------------------------------------------------- fixtures
def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"url": URL, "token": TOKEN}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def ha() -> FakeHA:
    return FakeHA()


@pytest.fixture
def make_client(ha: FakeHA):
    clients: list[HAClient] = []

    def _make(**overrides: Any) -> HAClient:
        c = HAClient(
            make_settings(**overrides),
            transport=httpx.MockTransport(ha.handler),
            ws_connect=ha.ws_connect,
        )
        clients.append(c)
        return c

    yield _make


@pytest.fixture
def make_ops(ha: FakeHA, make_client):
    def _make(**overrides: Any) -> Operations:
        client = make_client(**overrides)
        return Operations(client.settings, client)

    return _make

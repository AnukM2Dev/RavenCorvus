"""Async Home Assistant client.

REST (httpx) covers states, services, history, logbook, templates, events and
the error log. The registries (areas, devices, entities) are only exposed over
the WebSocket API, so a short-lived WebSocket session is opened on demand and
closed again; the REST connection pool is kept for the life of the client.
"""

from __future__ import annotations

import json
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import httpx

from .config import Settings


# --------------------------------------------------------------------------- errors
class HAError(RuntimeError):
    """Base class for Home Assistant errors surfaced to the agent."""


class HAConnectionError(HAError):
    """Could not reach Home Assistant (DNS, refused, timeout, TLS)."""


class HAAuthError(HAError):
    """Token rejected (401) or not permitted (403)."""


class HANotFoundError(HAError):
    """Entity, service or endpoint not found (404)."""


class HABadRequestError(HAError):
    """Home Assistant rejected the request payload (400/422)."""


class HAServerError(HAError):
    """Home Assistant returned a 5xx response."""


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code < 400:
        return
    body = resp.text.strip()[:500]
    if code == 401:
        raise HAAuthError("401 Unauthorized: HASS_TOKEN was rejected. Create a new long-lived access token.")
    if code == 403:
        raise HAAuthError("403 Forbidden: the token's user is not allowed to do this.")
    if code == 404:
        raise HANotFoundError(f"404 Not Found: {body or resp.request.url.path}")
    if code in (400, 422):
        raise HABadRequestError(f"{code} Bad Request: {body}")
    if code >= 500:
        raise HAServerError(f"{code} Home Assistant server error: {body}")
    raise HAError(f"HTTP {code}: {body}")


# --------------------------------------------------------------------------- websocket
WSConnect = Callable[..., Any]  # (url, *, verify_tls, timeout) -> async context manager


def _default_ws_connect(url: str, *, verify_tls: bool, timeout: float) -> Any:
    from websockets.asyncio.client import connect

    ssl_ctx: ssl.SSLContext | None = None
    if url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if not verify_tls:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
    return connect(url, ssl=ssl_ctx, open_timeout=timeout, max_size=None)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# --------------------------------------------------------------------------- client
class HAClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: WSConnect | None = None,
    ) -> None:
        self.settings = settings
        self._ws_connect = ws_connect or _default_ws_connect
        self._http = httpx.AsyncClient(
            base_url=settings.rest_base,
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Content-Type": "application/json",
            },
            timeout=settings.timeout,
            verify=settings.verify_tls,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ REST core
    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HAConnectionError(
                f"Timed out after {self.settings.timeout}s talking to {self.settings.url}"
            ) from exc
        except httpx.TransportError as exc:
            raise HAConnectionError(f"Cannot reach {self.settings.url}: {exc}") from exc
        _raise_for_status(resp)
        return resp

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._request(method, path, **kwargs)
        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise HAError(f"Expected JSON from {path}, got: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------ read: REST
    async def status(self) -> dict[str, Any]:
        """Connectivity check plus instance facts."""
        cfg = await self._json("GET", "/config")
        return {
            "ok": True,
            "version": cfg.get("version"),
            "location_name": cfg.get("location_name"),
            "state": cfg.get("state"),
            "time_zone": cfg.get("time_zone"),
            "unit_system": cfg.get("unit_system"),
            "components": len(cfg.get("components", [])),
            "url": self.settings.url,
            "read_only": self.settings.read_only,
        }

    async def get_states(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/states")

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/states/{quote(entity_id, safe='')}")

    async def get_services(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/services")

    async def get_history(
        self,
        entity_ids: list[str],
        *,
        hours: float = 24,
        start: str | None = None,
        end: str | None = None,
        minimal: bool = True,
    ) -> list[list[dict[str, Any]]]:
        now = datetime.now(timezone.utc)
        start_ts = start or _iso(now - timedelta(hours=hours))
        params: dict[str, Any] = {"filter_entity_id": ",".join(entity_ids)}
        if end:
            params["end_time"] = end
        if minimal:
            params["minimal_response"] = ""
            params["no_attributes"] = ""
        return await self._json("GET", f"/history/period/{quote(start_ts, safe=':')}", params=params)

    async def get_logbook(
        self,
        *,
        entity_id: str | None = None,
        hours: float = 24,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        start_ts = start or _iso(now - timedelta(hours=hours))
        params: dict[str, Any] = {}
        if entity_id:
            params["entity"] = entity_id
        if end:
            params["end_time"] = end
        return await self._json("GET", f"/logbook/{quote(start_ts, safe=':')}", params=params)

    async def render_template(self, template: str) -> str:
        resp = await self._request("POST", "/template", json={"template": template})
        return resp.text

    async def get_error_log(self) -> str:
        resp = await self._request("GET", "/error_log")
        return resp.text

    async def check_config(self) -> dict[str, Any]:
        return await self._json("POST", "/config/core/check_config")

    # ------------------------------------------------------------------ write: REST
    # NOTE: these methods do not enforce policy. The server/CLI call
    # safety.WriteGuard first, so the guard can be tested in isolation.
    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        *,
        return_response: bool = False,
    ) -> Any:
        """POST /api/services/<domain>/<service>.

        Without ``return_response`` HA answers with the list of states that
        changed. With it, HA answers {"changed_states": [...],
        "service_response": {...}}, which is normalized to that shape here.
        """
        params = {"return_response": ""} if return_response else None
        result = await self._json(
            "POST",
            f"/services/{quote(domain, safe='')}/{quote(service, safe='')}",
            json=data or {},
            params=params,
        )
        if return_response:
            return result or {"changed_states": [], "service_response": {}}
        return {"changed_states": result or [], "service_response": None}

    async def set_state(
        self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST /api/states/<entity_id>. Writes the state machine only; it does
        not command a device."""
        return await self._json(
            "POST",
            f"/states/{quote(entity_id, safe='')}",
            json={"state": state, "attributes": attributes or {}},
        )

    async def fire_event(self, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._json("POST", f"/events/{quote(event_type, safe='')}", json=data or {})

    # ------------------------------------------------------------------ WebSocket
    @asynccontextmanager
    async def _ws_session(self) -> AsyncIterator[Any]:
        try:
            cm = self._ws_connect(
                self.settings.ws_url,
                verify_tls=self.settings.verify_tls,
                timeout=self.settings.timeout,
            )
            async with cm as ws:
                hello = json.loads(await ws.recv())
                if hello.get("type") != "auth_required":
                    raise HAError(f"Unexpected WebSocket greeting: {hello}")
                await ws.send(json.dumps({"type": "auth", "access_token": self.settings.token}))
                reply = json.loads(await ws.recv())
                if reply.get("type") == "auth_invalid":
                    raise HAAuthError(f"WebSocket auth failed: {reply.get('message', 'invalid token')}")
                if reply.get("type") != "auth_ok":
                    raise HAError(f"Unexpected WebSocket auth reply: {reply}")
                yield ws
        except HAError:
            raise
        except OSError as exc:
            raise HAConnectionError(f"Cannot open WebSocket to {self.settings.ws_url}: {exc}") from exc
        except TimeoutError as exc:
            raise HAConnectionError("WebSocket connection timed out") from exc
        except Exception as exc:  # websockets raises its own hierarchy
            if exc.__class__.__module__.startswith("websockets"):
                raise HAConnectionError(f"WebSocket error: {exc}") from exc
            raise

    async def ws_commands(self, commands: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Run several WebSocket commands over a single short-lived session."""
        results: list[Any] = []
        async with self._ws_session() as ws:
            for i, (msg_type, payload) in enumerate(commands, start=1):
                await ws.send(json.dumps({"id": i, "type": msg_type, **payload}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") != i or msg.get("type") != "result":
                        continue  # events / unrelated frames
                    if not msg.get("success"):
                        err = msg.get("error", {})
                        raise HAError(
                            f"WebSocket command {msg_type} failed: "
                            f"{err.get('code', 'error')}: {err.get('message', '')}"
                        )
                    results.append(msg.get("result"))
                    break
        return results

    async def ws_command(self, msg_type: str, **payload: Any) -> Any:
        return (await self.ws_commands([(msg_type, payload)]))[0]

    # ------------------------------------------------------------------ automations / traces
    async def automation_config(self, entity_id: str) -> dict[str, Any]:
        result = await self.ws_command("automation/config", entity_id=entity_id)
        return (result or {}).get("config", result or {})

    async def list_traces(self, domain: str, item_id: str) -> list[dict[str, Any]]:
        return await self.ws_command("trace/list", domain=domain, item_id=item_id) or []

    async def get_trace(self, domain: str, item_id: str, run_id: str) -> dict[str, Any]:
        return await self.ws_command("trace/get", domain=domain, item_id=item_id, run_id=run_id)

    # ------------------------------------------------------------------ registries
    async def list_areas(self) -> list[dict[str, Any]]:
        return await self.ws_command("config/area_registry/list")

    async def list_devices(self) -> list[dict[str, Any]]:
        return await self.ws_command("config/device_registry/list")

    async def list_entity_registry(self) -> list[dict[str, Any]]:
        return await self.ws_command("config/entity_registry/list")

    async def registries(self) -> dict[str, list[dict[str, Any]]]:
        areas, devices, entities = await self.ws_commands(
            [
                ("config/area_registry/list", {}),
                ("config/device_registry/list", {}),
                ("config/entity_registry/list", {}),
            ]
        )
        return {"areas": areas, "devices": devices, "entities": entities}

    async def entity_area_map(self) -> dict[str, dict[str, str]]:
        """entity_id -> {"area_id", "area"}; an entity inherits its device's
        area unless it has an override of its own."""
        reg = await self.registries()
        area_names = {a["area_id"]: a.get("name", a["area_id"]) for a in reg["areas"]}
        device_area = {d["id"]: d.get("area_id") for d in reg["devices"]}
        out: dict[str, dict[str, str]] = {}
        for ent in reg["entities"]:
            area_id = ent.get("area_id") or device_area.get(ent.get("device_id"))
            if area_id:
                out[ent["entity_id"]] = {"area_id": area_id, "area": area_names.get(area_id, area_id)}
        return out

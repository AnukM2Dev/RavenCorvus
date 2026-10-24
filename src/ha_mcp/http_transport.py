"""Remote (streamable HTTP) transport with a shared-secret gate.

The stdio server is the default. This module serves the same tool surface over
MCP streamable HTTP so it can sit behind a tunnel, and refuses every request
that does not carry the operator's key.

The key is accepted two ways, because MCP clients differ in what they can send:

- ``Authorization: Bearer <key>`` (clients that support custom headers), or
- a secret path prefix: ``https://host/<key>/mcp`` (clients that only take a URL,
  such as claude.ai custom connectors).

``GET /healthz`` is the only unauthenticated route and reveals nothing.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import ConfigError

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MIN_KEY_LENGTH = 32
DEFAULT_PORT = 8765


@dataclass(frozen=True)
class HttpSettings:
    api_key: str
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    path: str = "/mcp"
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "HttpSettings":
        env = dict(os.environ) if env is None else env
        key = env.get("RAVEN_API_KEY", "").strip()
        if not key:
            raise ConfigError(
                "RAVEN_API_KEY is required for the http transport "
                "(generate one: python -c \"import secrets;print(secrets.token_urlsafe(32))\")"
            )
        if len(key) < MIN_KEY_LENGTH:
            raise ConfigError(f"RAVEN_API_KEY must be at least {MIN_KEY_LENGTH} characters")
        if "/" in key or "?" in key or "#" in key:
            raise ConfigError("RAVEN_API_KEY must be URL-safe (no '/', '?' or '#')")
        try:
            port = int(env.get("RAVEN_PORT", DEFAULT_PORT))
        except ValueError:
            raise ConfigError("RAVEN_PORT must be an integer") from None
        hosts = [h.strip() for h in env.get("RAVEN_ALLOWED_HOSTS", "").split(",") if h.strip()]
        return cls(
            api_key=key,
            host=env.get("RAVEN_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=port,
            allowed_hosts=hosts,
        )


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def _respond(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class KeyGate:
    """ASGI middleware: allow a request only if it carries the key."""

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self.api_key = api_key
        self._prefix = f"/{api_key}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await _respond(send, 404, b"not found")
            return

        path: str = scope.get("path", "")
        if path == "/healthz":
            await _respond(send, 200, b"ok")
            return

        auth = _header(scope, b"authorization")
        if auth.lower().startswith("bearer ") and _eq(auth[7:].strip(), self.api_key):
            await self.app(scope, receive, send)
            return

        head, sep, rest = path[1:].partition("/")
        if _eq(head, self.api_key):
            inner = dict(scope)
            inner["path"] = "/" + rest if sep else "/"
            inner["raw_path"] = inner["path"].encode()
            inner["root_path"] = scope.get("root_path", "") + self._prefix
            await self.app(inner, receive, send)
            return

        await _respond(send, 401, b"unauthorized")


def build_http_app(server: Any, settings: HttpSettings) -> ASGIApp:
    """Wrap the MCP server's streamable HTTP app in the key gate."""
    kwargs: dict[str, Any] = {"streamable_http_path": settings.path, "stateless_http": True}
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # pragma: no cover - very old mcp
        TransportSecuritySettings = None  # type: ignore[assignment]
    if TransportSecuritySettings is not None:
        if settings.allowed_hosts:
            kwargs["transport_security"] = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=settings.allowed_hosts,
                allowed_origins=[f"https://{h}" for h in settings.allowed_hosts],
            )
        else:
            # Behind a tunnel the Host header is the public name; the key gate is the control.
            kwargs["transport_security"] = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
    try:
        app = server.streamable_http_app(**kwargs)
    except TypeError:  # mcp 1.x: settings live on the server object
        server.settings.streamable_http_path = settings.path
        server.settings.stateless_http = True
        app = server.streamable_http_app()
    return KeyGate(app, settings.api_key)


def serve(server: Any, settings: HttpSettings) -> None:
    import uvicorn

    uvicorn.run(
        build_http_app(server, settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,  # the path can carry the key; never log it
        proxy_headers=True,
        forwarded_allow_ips="*",
    )

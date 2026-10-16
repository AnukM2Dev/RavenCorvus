"""StreamableHTTP transport with mandatory authentication.

A public endpoint that can control a house must never be open, so this refuses to start
unless at least one of these is configured (both may be):

- ``HA_MCP_PATH_SECRET``: the endpoint is served at ``/<secret>/mcp``; any other path is a 404.
  This is what to use for claude.ai custom connectors, which cannot send custom headers.
- ``HA_MCP_AUTH_TOKEN``: requests must carry ``Authorization: Bearer <token>``. Use this for
  clients that can set headers (Claude Code, mcp-remote, curl).

Optional: ``HA_MCP_ALLOWED_HOSTS`` (comma list of public hostnames). When set, DNS-rebinding
protection admits only those Host headers; when unset it is disabled, because a tunnel forwards
the public hostname and authentication is already mandatory.
"""

from __future__ import annotations

import hmac
import os
import re

from .config import ConfigError

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")


def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


class HttpAuthConfig:
    def __init__(self, token: str = "", path_secret: str = "", allowed_hosts: list[str] | None = None):
        self.token = token
        self.path_secret = path_secret
        self.allowed_hosts = allowed_hosts or []

    @classmethod
    def from_env(cls) -> "HttpAuthConfig":
        cfg = cls(
            token=os.environ.get("HA_MCP_AUTH_TOKEN", "").strip(),
            path_secret=os.environ.get("HA_MCP_PATH_SECRET", "").strip().strip("/"),
            allowed_hosts=_csv(os.environ.get("HA_MCP_ALLOWED_HOSTS", "")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.token and not self.path_secret:
            raise ConfigError(
                "HTTP transport needs authentication: set HA_MCP_PATH_SECRET (claude.ai connector) "
                "and/or HA_MCP_AUTH_TOKEN (bearer header). Refusing to serve an open endpoint."
            )
        if self.path_secret and not _SECRET_RE.match(self.path_secret):
            raise ConfigError(
                "HA_MCP_PATH_SECRET must be 16+ characters of letters, digits, '-' or '_' "
                "(generate one with: python -c \"import secrets;print(secrets.token_urlsafe(32))\")"
            )
        if self.token and len(self.token) < 16:
            raise ConfigError("HA_MCP_AUTH_TOKEN must be at least 16 characters")


class AuthMiddleware:
    """Pure ASGI middleware: path-secret gate, then bearer check. Failures reveal nothing."""

    def __init__(self, app, cfg: HttpAuthConfig):  # noqa: ANN001
        self.app = app
        self.cfg = cfg

    async def _reply(self, send, status: int, headers: list | None = None) -> None:  # noqa: ANN001
        await send({"type": "http.response.start", "status": status,
                    "headers": headers or [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if self.cfg.path_secret:
            prefix = f"/{self.cfg.path_secret}"
            # constant-time compare of the first path segment
            first = path.split("/", 2)[1] if path.startswith("/") and len(path) > 1 else ""
            if not hmac.compare_digest(first.encode(), self.cfg.path_secret.encode()):
                await self._reply(send, 404)
                return
            scope = dict(scope)
            scope["path"] = path[len(prefix):] or "/"
            raw = scope.get("raw_path")
            if raw:
                scope["raw_path"] = raw[len(prefix.encode()):] or b"/"

        if self.cfg.token:
            supplied = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    supplied = v.decode("latin-1")
                    break
            expected = f"Bearer {self.cfg.token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                await self._reply(
                    send, 401,
                    [(b"content-length", b"0"), (b"www-authenticate", b'Bearer realm="ha-mcp"')],
                )
                return

        await self.app(scope, receive, send)


def build_app(server, cfg: HttpAuthConfig):  # noqa: ANN001
    """Wrap the MCP server's streamable-HTTP Starlette app with authentication."""
    try:  # mcp 2.x
        from mcp.server.transport_security import TransportSecuritySettings

        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(cfg.allowed_hosts),
            allowed_hosts=cfg.allowed_hosts,
            allowed_origins=[],
        )
        inner = server.streamable_http_app(transport_security=security)
    except TypeError:  # mcp 1.x takes no arguments
        inner = server.streamable_http_app()
    return AuthMiddleware(inner, cfg)


def serve(server, cfg: HttpAuthConfig, host: str, port: int) -> None:  # noqa: ANN001
    import uvicorn

    app = build_app(server, cfg)
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")

"""Remote (streamable HTTP) transport, gated.

The stdio server is the default. This module serves the same tool surface over
MCP streamable HTTP so it can sit behind a Cloudflare Tunnel.

A request is let through when any enabled check passes:

1. **Cloudflare Access** (recommended for claude.ai). With ``RAVEN_ACCESS_TEAM`` and
   ``RAVEN_ACCESS_AUD`` set, a valid ``Cf-Access-Jwt-Assertion`` header, which Access
   adds after the user signs in, is verified against the team's signing keys. Access
   handles the OAuth sign-in for MCP clients at the edge, so no secret lives in a URL.
2. **Bearer key** (CLI, scripts, Claude Code): ``Authorization: Bearer <RAVEN_API_KEY>``.
3. **Path key** (off by default): ``/<RAVEN_API_KEY>/mcp`` for URL-only clients. Enable
   it only with ``RAVEN_PATH_KEY=1``. Anthropic's connector guidance says credentials
   must not travel in URLs, because URLs land in proxy logs.

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
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HttpSettings:
    api_key: str
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    path: str = "/mcp"
    allowed_hosts: list[str] = field(default_factory=list)
    path_key: bool = False
    access_team: str = ""  # e.g. "ravenswood" or "ravenswood.cloudflareaccess.com"
    access_aud: str = ""

    @property
    def access_enabled(self) -> bool:
        return bool(self.access_team and self.access_aud)

    @property
    def access_issuer(self) -> str:
        team = self.access_team.strip().removeprefix("https://").rstrip("/")
        if "." not in team:
            team = f"{team}.cloudflareaccess.com"
        return f"https://{team}"

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
        team = env.get("RAVEN_ACCESS_TEAM", "").strip()
        aud = env.get("RAVEN_ACCESS_AUD", "").strip()
        if bool(team) != bool(aud):
            raise ConfigError("Set both RAVEN_ACCESS_TEAM and RAVEN_ACCESS_AUD, or neither")
        return cls(
            api_key=key,
            host=env.get("RAVEN_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=port,
            allowed_hosts=hosts,
            path_key=env.get("RAVEN_PATH_KEY", "").strip().lower() in _TRUE,
            access_team=team,
            access_aud=aud,
        )


# --------------------------------------------------------------------------- Access JWT
class AccessVerifier:
    """Verify Cloudflare Access application tokens (RS256, team JWKS)."""

    def __init__(self, issuer: str, audience: str, jwks_client: Any | None = None) -> None:
        import jwt

        self._jwt = jwt
        self.issuer = issuer
        self.audience = audience
        self._jwks = jwks_client or jwt.PyJWKClient(
            f"{issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
        )

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            return self._jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception:  # any failure = not authenticated
            return None


# --------------------------------------------------------------------------- gate
def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def _respond(send: Send, status: int, body: bytes, extra: list | None = None) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8"), *(extra or [])]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class KeyGate:
    """ASGI middleware: allow a request only if an enabled check passes."""

    def __init__(
        self,
        app: ASGIApp,
        api_key: str,
        *,
        path_key: bool = False,
        verifier: AccessVerifier | None = None,
    ) -> None:
        self.app = app
        self.api_key = api_key
        self.path_key = path_key
        self.verifier = verifier
        self._prefix = f"/{api_key}"

    async def _access_ok(self, scope: Scope) -> bool:
        if self.verifier is None:
            return False
        token = _header(scope, b"cf-access-jwt-assertion")
        if not token:
            return False
        import anyio

        return await anyio.to_thread.run_sync(self.verifier.verify, token) is not None

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

        if await self._access_ok(scope):
            await self.app(scope, receive, send)
            return

        auth = _header(scope, b"authorization")
        if auth.lower().startswith("bearer ") and _eq(auth[7:].strip(), self.api_key):
            await self.app(scope, receive, send)
            return

        if self.path_key:
            head, sep, rest = path[1:].partition("/")
            if _eq(head, self.api_key):
                inner = dict(scope)
                inner["path"] = "/" + rest if sep else "/"
                inner["raw_path"] = inner["path"].encode()
                inner["root_path"] = scope.get("root_path", "") + self._prefix
                await self.app(inner, receive, send)
                return

        await _respond(
            send, 401, b"unauthorized", [(b"www-authenticate", b'Bearer realm="ravencorvus"')]
        )


def build_http_app(
    server: Any, settings: HttpSettings, *, verifier: AccessVerifier | None = None
) -> ASGIApp:
    """Wrap the MCP server's streamable HTTP app in the gate."""
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
            # Behind a tunnel the Host header is the public name; the gate is the control.
            kwargs["transport_security"] = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
    try:
        app = server.streamable_http_app(**kwargs)
    except TypeError:  # mcp 1.x: settings live on the server object
        server.settings.streamable_http_path = settings.path
        server.settings.stateless_http = True
        app = server.streamable_http_app()
    if verifier is None and settings.access_enabled:
        verifier = AccessVerifier(settings.access_issuer, settings.access_aud)
    return KeyGate(app, settings.api_key, path_key=settings.path_key, verifier=verifier)


def serve(server: Any, settings: HttpSettings) -> None:
    import sys

    import uvicorn

    modes = ["bearer key"]
    if settings.access_enabled:
        modes.insert(0, f"Cloudflare Access ({settings.access_issuer})")
    if settings.path_key:
        modes.append("path key (RAVEN_PATH_KEY=1; not recommended)")
    print(f"ha-mcp: auth = {', '.join(modes)}", file=sys.stderr)

    uvicorn.run(
        build_http_app(server, settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,  # the path can carry the key; never log it
        proxy_headers=True,
        forwarded_allow_ips="*",
    )

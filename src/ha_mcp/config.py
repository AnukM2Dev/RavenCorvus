"""Environment-driven settings.

The operator, not the agent, sets the safety boundary. Everything here is read
from environment variables so it can be pinned in the MCP server config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Domains where a wrong call has physical-security or safety consequences.
DEFAULT_SENSITIVE_DOMAINS: frozenset[str] = frozenset(
    {"lock", "alarm_control_panel", "cover", "climate", "valve"}
)

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _csv(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    url: str
    token: str = field(repr=False)
    verify_tls: bool = True
    timeout: float = 15.0
    read_only: bool = False
    allowed_domains: frozenset[str] = frozenset()  # empty = all non-sensitive
    denied_entities: frozenset[str] = frozenset()
    allow_sensitive: frozenset[str] = frozenset()  # domains opted in
    sensitive_domains: frozenset[str] = DEFAULT_SENSITIVE_DOMAINS

    @property
    def rest_base(self) -> str:
        return f"{self.url}/api"

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.url.startswith("https://") else "ws"
        host = self.url.split("://", 1)[1]
        if host.endswith("/core"):  # HA Supervisor proxy (add-on): ws://supervisor/core/websocket
            return f"{scheme}://{host}/websocket"
        return f"{scheme}://{host}/api/websocket"

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("HASS_URL", "").strip().rstrip("/")
        token = os.environ.get("HASS_TOKEN", "").strip()
        if not url:
            raise ConfigError("HASS_URL is not set (e.g. http://homeassistant.local:8123)")
        if not token:
            raise ConfigError("HASS_TOKEN is not set (create a long-lived access token)")
        if not url.startswith(("http://", "https://")):
            raise ConfigError("HASS_URL must start with http:// or https://")

        try:
            timeout = float(os.environ.get("HASS_TIMEOUT", "15"))
        except ValueError as exc:
            raise ConfigError("HASS_TIMEOUT must be a number of seconds") from exc

        raw_sensitive = os.environ.get("HASS_ALLOW_SENSITIVE_DOMAINS", "").strip().lower()
        if raw_sensitive in _TRUE or raw_sensitive == "all":
            allow_sensitive = DEFAULT_SENSITIVE_DOMAINS
        else:
            allow_sensitive = _csv("HASS_ALLOW_SENSITIVE_DOMAINS")

        return cls(
            url=url,
            token=token,
            verify_tls=_flag("HASS_VERIFY_TLS", True),
            timeout=timeout,
            read_only=_flag("HASS_READ_ONLY", False),
            allowed_domains=_csv("HASS_ALLOWED_DOMAINS"),
            denied_entities=_csv("HASS_DENIED_ENTITIES"),
            allow_sensitive=allow_sensitive,
        )

# RavenCorvus

An MCP server (stdio) and a companion `ha` CLI that give an agent read/write access to a
[Home Assistant](https://www.home-assistant.io/) instance, with a write guard the **operator** controls.

## Why an MCP server

Home Assistant exposes two relevant APIs:

- **REST** (`/api/...`): states, services, history, logbook, template rendering, events, error log.
- **WebSocket** (`/api/websocket`): the only way to read the area / device / entity registries, which is
  what makes "turn off the lights in the kitchen" resolvable.

This package uses both. REST goes over one pooled `httpx` client; the registries are read over a short-lived
WebSocket session opened on demand. The `ha` CLI exposes the same operations so the capability is
reachable from a bare shell when MCP is not wired up.

## Install

```bash
cd RavenCorvus
uv sync            # Python >=3.11
uv run ha --help
```

Works with `mcp` 1.x (`FastMCP`) and 2.x (`MCPServer`); the server picks whichever is installed.

## Create a token

1. In Home Assistant, open your **user profile** (bottom of the sidebar) → **Security**.
2. Under **Long-lived access tokens**, choose **Create token**, name it (e.g. `agent`), and copy it. It is
   shown once.
3. Put it in `HASS_TOKEN`. Never commit it.

A token acts with the permissions of the user that created it. For anything beyond experiments, create a
**dedicated user** for the agent (non-administrator) and create the token from that account, so Home
Assistant's own permissions back up the settings below.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `HASS_URL` | required | Base URL, e.g. `http://homeassistant.local:8123` |
| `HASS_TOKEN` | required | Long-lived access token |
| `HASS_VERIFY_TLS` | `1` | Set `0` for self-signed certificates |
| `HASS_TIMEOUT` | `15` | Seconds, per request and for WebSocket connect |
| `HASS_READ_ONLY` | off | `1` removes every write tool |
| `HASS_ALLOWED_DOMAINS` | all non-sensitive | Comma list; when set, only these domains can be written |
| `HASS_DENIED_ENTITIES` | none | Comma list of entity ids or globs (`lock.*`, `switch.server_*`) that are never written |
| `HASS_ALLOW_SENSITIVE_DOMAINS` | none | Opt in to sensitive domains: a comma list (`climate,cover`) or `all` |

## Safety model

The default posture is permissive for ordinary domains but blocks sensitive ones until the operator opts in,
because an agent with an HA token can unlock doors. Sensitive domains: `lock`, `alarm_control_panel`,
`cover`, `climate`, `valve`.

Checks run in this order and the first failure wins:

1. `HASS_READ_ONLY`: write tools are not even registered, so the agent never sees an action it cannot take.
2. `HASS_DENIED_ENTITIES`: beats every opt-in. Patterns are matched against the entity ids in the call **and**
   against the `domain.service` name, so `script.*` also stops a direct `script.my_script` call (HA exposes
   scripts as services with no entity id in the payload).
3. Sensitive domains: need `HASS_ALLOW_SENSITIVE_DOMAINS`. Checked against the service domain **and** every
   entity named in the payload, so `script.run` naming `lock.front_door` is still blocked.
4. `HASS_ALLOWED_DOMAINS`: narrows the surface. It never unlocks a sensitive domain by itself.

A blocked call fails with an explanatory error naming the setting that would permit it, e.g.:

```
BLOCKED: Domain 'lock' is sensitive and blocked by default. Set HASS_ALLOW_SENSITIVE_DOMAINS=lock (or 'all') to allow it.
```

Notes on the edges:

- Generic fan-out services (`homeassistant.turn_off`, ...) given an area/device/label target, or no explicit
  entity ids, could reach sensitive domains without naming them, so they are blocked unless **all** sensitive
  domains are opted in. Name explicit `entity_id`s, or use a domain-specific service
  (`light.turn_off` with an `area_id` is fine; it can only reach lights).
- `fire_event` can trigger arbitrary automations, so it needs the full sensitive opt-in.
- `set_state` only edits HA's state machine (it does not command a device) but gets the same checks, since a
  forged lock or alarm state misleads automations and people.
- **The guard cannot see inside `script.*`, `automation.*`, `scene.*` or `shell_command.*`.** A script can do
  anything its author wrote. If you do not want the agent running them, leave them out of
  `HASS_ALLOWED_DOMAINS` or add them to `HASS_DENIED_ENTITIES` (e.g. `script.*`).

## Tools

**Read:** `ha_status`, `list_entities` (filter by domain / area / substring), `get_state`, `search_entities`,
`list_services`, `list_areas`, `list_devices`, `get_history`, `get_logbook`, `render_template`,
`get_error_log`.

**Write:** `call_service` (the single action path, with `dry_run`), `set_state`, `fire_event`.

Listings are compacted (id, state, name, area, a few useful attributes) so they do not flood the agent's
context; `get_state(full=true)` returns every attribute. Write tools return the entity states that actually
changed (`from` -> `to`). An empty `changed` list is reported explicitly so the agent verifies with
`get_state` instead of assuming the action landed.

`call_service(..., dry_run=true)` sends nothing. It reports whether policy allows the call (with the
reason if not), whether the service exists, and the current state of each target.

## Use with Warp

**Local Warp agents**: add to `~/.warp/.mcp.json` (standard `mcpServers` stdio shape):

```json
{
  "mcpServers": {
    "homeassistant": {
      "command": "uv",
      "args": ["run", "--quiet", "--project", "/absolute/path/to/RavenCorvus", "ha-mcp"],
      "env": {
        "HASS_URL": "http://homeassistant.local:8123",
        "HASS_TOKEN": "<long-lived access token>",
        "HASS_READ_ONLY": "1"
      }
    }
  }
}
```

Start with `HASS_READ_ONLY=1`, confirm reads work, then remove it and add only the opt-ins you want
(for example `"HASS_DENIED_ENTITIES": "lock.*,script.*"`). The token sits in that file in plain text, so
keep the file private. If your Warp version expects a different schema, adjust the wrapper only; the
command, args and environment are what matter.

**Cloud agents**: pass the same server definition with `--mcp`. A cloud agent runs outside your network, so
`HASS_URL` must be reachable from the internet (Home Assistant Cloud / Nabu Casa remote URL, or a tunnel).
`homeassistant.local` will not resolve there. Treat an internet-reachable URL plus a token as sensitive and
use a dedicated low-privilege user.

## Remote access (StreamableHTTP)

For claude.ai / the mobile app / cloud agents, run the HTTP transport and put a tunnel in front of it.
It **refuses to start without authentication**:

| Variable | Purpose |
|---|---|
| `HA_MCP_PATH_SECRET` | Endpoint is served at `/<secret>/mcp`, every other path is 404. Use this for claude.ai custom connectors (they cannot send headers). 16+ chars. |
| `HA_MCP_AUTH_TOKEN` | Also require `Authorization: Bearer <token>`. For Claude Code / `mcp-remote` / curl. 16+ chars. |
| `HA_MCP_ALLOWED_HOSTS` | Optional comma list of public hostnames for DNS-rebinding protection. |

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"   # generate a secret
export HA_MCP_PATH_SECRET=...
uv run ha-mcp --transport http --host 127.0.0.1 --port 8787
# connector URL: https://ha-mcp.example.com/<secret>/mcp
```

Keep `--host 127.0.0.1`; the tunnel connects to it locally. Start with `HASS_READ_ONLY=1`.
The secret is part of the URL, so treat the URL like a password and prefer adding Cloudflare Access
or the bearer token on top.

## Home Assistant OS add-on (recommended for remote access)

On Home Assistant OS there is no host shell, so RavenCorvus ships as a local add-on that runs `ha-mcp`
(HTTP, loopback only) and `cloudflared` together. The repo root *is* the add-on (`config.yaml`,
`Dockerfile`, `run.sh`).

1. **Install:** with the Samba share add-on enabled, clone into the `addons` share:
   `git clone -b http-transport https://github.com/AnukM2Dev/RavenCorvus.git \\homeassistant.local\addons\ravencorvus`
   (or copy the repo folder there). In HA: Settings > Add-ons > Add-on store > menu > Check for updates;
   RavenCorvus appears under *Local add-ons*. Install it.
2. **Create a Cloudflare tunnel** (Zero Trust > Networks > Tunnels) with a published application routing your
   hostname to `http://localhost:8787`, and copy its connector token.
3. **Create an HA token:** profile > Security > Long-lived access tokens (preferably from a dedicated non-admin user).
4. **Configure the add-on:** set `tunnel_token`, `ha_token`, `public_hostname`. Leave `path_secret` empty to have
   one generated and kept across restarts. Leave `read_only` on to start.
5. **Start it** and open the Log tab: it prints the connector URL `https://<host>/<secret>/mcp`.
   Treat that URL like a password and add it in claude.ai under Settings > Connectors > Add custom connector.

Options: `denied_entities`, `allowed_domains`, `allow_sensitive_domains` map to the `HASS_*` variables above.
The add-on reaches HA at `http://homeassistant:8123` on the internal add-on network (`ha_url`).

## CLI

```bash
export HASS_URL=http://homeassistant.local:8123 HASS_TOKEN=...

ha status                               # live smoke test: connectivity + HA version
ha entities --domain light --area kitchen
ha state light.kitchen --full
ha search "attic fan"
ha services --domain light
ha areas ; ha devices --area kitchen
ha history sensor.outdoor_temp --hours 6
ha logbook --entity light.kitchen
ha template '{{ states("sensor.outdoor_temp") }}'
ha error-log --lines 50

ha call light turn_on -e light.kitchen -d '{"brightness": 128}' --dry-run
ha call light turn_on -e light.kitchen -d '{"brightness": 128}'
```

`-f json` forces JSON everywhere (pipe to `jq`). Listings default to tables. Exit codes: `0` ok, `1` Home
Assistant error, `2` blocked by policy or bad configuration/arguments.

## Verify against your instance

The test suite uses a fake Home Assistant, so run this once against the real one:

```bash
HASS_READ_ONLY=1 ha status
```

Expected output is JSON with `"ok": true`, your HA version and location name. `401` means the token is
wrong; "Cannot reach" means the URL or network is wrong.

## Development

```bash
uv run pytest          # 106 tests, no network needed
```

- `tests/conftest.py`: `FakeHA`, an `httpx.MockTransport` fake of the REST API plus a fake WebSocket for the
  registries.
- `tests/test_safety.py`, `tests/test_formatting.py`: unit tests for the guard and formatters.
- `tests/test_client.py`, `test_ops.py`, `test_server.py`, `test_cli.py`, `test_config.py`: the client,
  shared operations, MCP tool surface (including read-only registration), CLI exit codes, and settings.

Layout:

```
src/ha_mcp/
  config.py      environment-driven settings
  client.py      async httpx REST client + short-lived WebSocket registry reads, typed errors
  safety.py      write guard
  formatting.py  state/history/log compaction
  ops.py         operations shared by the server and the CLI
  server.py      MCP tool surface (ha-mcp)
  cli.py         the `ha` command
```

#!/bin/bash
# Home Assistant add-on entrypoint: runs ha-mcp (HTTP, loopback) and cloudflared side by side.
set -euo pipefail

OPTS=/data/options.json
opt() { jq -r --arg k "$1" '.[$k] // ""' "$OPTS"; }

TUNNEL_TOKEN="$(opt tunnel_token)"
HA_TOKEN="$(opt ha_token)"
HOST="$(opt public_hostname)"
SECRET="$(opt path_secret)"

if [ -z "$TUNNEL_TOKEN" ] || [ -z "$HA_TOKEN" ] || [ -z "$HOST" ]; then
  echo "ERROR: tunnel_token, ha_token and public_hostname must all be set in the add-on configuration." >&2
  exit 1
fi

# Generate a path secret once and keep it across restarts if none was configured.
if [ -z "$SECRET" ]; then
  if [ ! -s /data/path_secret ]; then
    python -c 'import secrets;print(secrets.token_urlsafe(32))' > /data/path_secret
    chmod 600 /data/path_secret
  fi
  SECRET="$(cat /data/path_secret)"
fi

export HASS_URL="$(opt ha_url)"
export HASS_TOKEN="$HA_TOKEN"
export HA_MCP_PATH_SECRET="$SECRET"
export HA_MCP_ALLOWED_HOSTS="$HOST"
[ "$(opt read_only)" = "true" ] && export HASS_READ_ONLY=1
export HASS_DENIED_ENTITIES="$(opt denied_entities)"
export HASS_ALLOWED_DOMAINS="$(opt allowed_domains)"
export HASS_ALLOW_SENSITIVE_DOMAINS="$(opt allow_sensitive_domains)"

echo "RavenCorvus connector URL (keep private): https://${HOST}/${SECRET}/mcp"
if [ "${HASS_READ_ONLY:-}" = "1" ]; then echo "Mode: READ-ONLY"; else echo "Mode: READ/WRITE (guard active)"; fi

ha-mcp --transport http --host 127.0.0.1 --port 8787 &
MCP_PID=$!

# Pass the token via the environment, not the process list.
TUNNEL_TOKEN="$TUNNEL_TOKEN" cloudflared tunnel --no-autoupdate run &
CF_PID=$!

# If either process exits, stop the other and let the supervisor restart the add-on.
trap 'kill "$MCP_PID" "$CF_PID" 2>/dev/null || true' TERM INT
wait -n "$MCP_PID" "$CF_PID"
STATUS=$?
echo "A process exited (status $STATUS); stopping." >&2
kill "$MCP_PID" "$CF_PID" 2>/dev/null || true
exit "$STATUS"

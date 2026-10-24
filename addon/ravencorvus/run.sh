#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

join() { bashio::config "$1" | paste -sd, -; }

if ! bashio::config.has_value 'ha_token'; then
  bashio::exit.nok "ha_token is required: a long-lived token from a dedicated, non-admin HA user."
fi
if ! bashio::config.has_value 'api_key'; then
  bashio::exit.nok "api_key is required (32+ URL-safe chars)."
fi

export HASS_URL="http://homeassistant:8123"
export HASS_TOKEN="$(bashio::config 'ha_token')"
export RAVEN_API_KEY="$(bashio::config 'api_key')"
export RAVEN_HOST="0.0.0.0"
export RAVEN_PORT="8765"

bashio::config.true 'read_only' && export HASS_READ_ONLY=1
bashio::config.true 'path_key' && export RAVEN_PATH_KEY=1
export HASS_ALLOWED_DOMAINS="$(join 'allowed_domains[]')"
export HASS_DENIED_ENTITIES="$(join 'denied_entities[]')"
export HASS_ALLOW_SENSITIVE_DOMAINS="$(join 'allow_sensitive_domains[]')"
export RAVEN_ALLOWED_HOSTS="$(join 'allowed_hosts[]')"

if bashio::config.has_value 'access_team'; then
  export RAVEN_ACCESS_TEAM="$(bashio::config 'access_team')"
  export RAVEN_ACCESS_AUD="$(bashio::config 'access_aud')"
fi

bashio::log.info "Starting RavenCorvus (read_only=$(bashio::config 'read_only'))"
exec ha-mcp --transport http

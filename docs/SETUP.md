# RavenCorvus remote setup (add-on 0.3.0)

Claude (web, desktop, iPhone, iPad) → `https://rc.ravenswoodfarmstead.com/mcp` → **Cloudflare Access**
sign-in → the tunnel built into the add-on → `ha-mcp` on loopback → Home Assistant through the Supervisor proxy.

**Cost:** $0. Cloudflare Zero Trust Free covers 50 users. No Nabu Casa dependency for this path.

The add-on is installed from the app repository `https://github.com/AnukM2Dev/RavenCorvus#http-transport`.
HA builds whatever is on that branch, so **an update only reaches the box after the branch is pushed**.

## Already done (0.2.0, running)

- [x] Add-on installed and started. Tunnel registered (iad).
- [x] `read_only: true`, `use_supervisor_api: true`.
- [x] Path-secret connector URL available in the add-on log.

## 1. Ship 0.3.0

- [ ] Merge `access-diagnostics` into `http-transport` and push it (GitHub Desktop).
- [ ] HA → Settings → Apps → App store → ⋮ → **Check for updates** → RavenCorvus → **Update** (rebuilds on the box).
- [ ] The log still shows `Auth: path secret ...`. Nothing changes until step 2 is done.

## 2. Cloudflare Access in front of the hostname

- [ ] Zero Trust → Access → Applications → **Add an application** → **Self-hosted**
  - Domain: `rc.ravenswoodfarmstead.com`
  - Policy: **Allow**, Include → Emails → your address
  - Login method: One-time PIN (or Google)
  - Turn on **Managed OAuth** for MCP clients if your dashboard shows it (Cloudflare added this in 2025;
    the label may differ). This is what lets Claude sign in.
- [ ] Copy the **Application Audience (AUD) tag** from the app's Overview.
- [ ] Team name: Zero Trust → Settings → Custom pages (`<team>.cloudflareaccess.com`).
- [ ] Add-on Configuration: set `access_team` and `access_aud`, then Save and Restart.
      The log should read `Auth: Cloudflare Access (<team>). Connector URL: https://rc.ravenswoodfarmstead.com/mcp`.

## 3. Allow only Anthropic at the edge

Anthropic's connector traffic comes from `160.79.104.0/21`.

- [ ] Cloudflare → ravenswoodfarmstead.com → Security → WAF → Custom rules → **Create rule**
  - Name: `RavenCorvus - Anthropic only`
  - Expression: `(http.host eq "rc.ravenswoodfarmstead.com" and not ip.src in {160.79.104.0/21})`
  - Action: **Block**

## 4. Reconnect Claude

- [ ] claude.ai → Settings → Connectors → remove the old RavenCorvus connector (the one with the secret in its URL).
- [ ] **Add custom connector** → `https://rc.ravenswoodfarmstead.com/mcp` → sign in through Access when prompted.
- [ ] Tool permissions: write tools (`call_service`, `set_state`, `fire_event`, `create_backup`) → **Ask every time**.
- [ ] Smoke test: "Check my Home Assistant status", "Show the last trace for the night path automation".
- [ ] Once this works, rotate the path secret: set `path_secret` to a new value (or clear it and delete
      `/data/path_secret` by reinstalling), so the old URL is dead.

**If Claude can't complete the Access sign-in**, keep the secret-path connector and the WAF rule from step 3,
and leave `access_team`/`access_aud` empty. Don't weaken Access.

## 5. When you're ready for writes

- `read_only: false`
- Locks and alarm stay blocked until you list them in `allow_sensitive_domains` (`lock,alarm_control_panel`).
- `denied_entities` for anything that must never be touched.

## New tools in 0.3.0

`check_config`, `get_automation_config`, `list_automation_traces` and `get_automation_trace` (read), and
`create_backup` (write; hidden while `read_only` is on). Traces need the automation to have an `id`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Update doesn't appear | Was the branch pushed? Check for updates again. The version must be higher than the installed one. |
| Claude "Couldn't reach the MCP server" | WAF rule, Access app domain, Managed OAuth toggle, tunnel status in Zero Trust. |
| Reaches the add-on but gets 401 | `access_team`/`access_aud` don't match. Copy the AUD again. |
| 404 on `/mcp` | Access isn't configured in the add-on, so the path secret is still required. |
| Traces say "no 'id' attribute" | That YAML automation has no `id:`. Add one and reload. |

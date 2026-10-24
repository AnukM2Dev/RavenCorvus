# RavenCorvus remote setup

Claude (web, desktop, iPhone, iPad) → `mcp.ravenswoodfarmstead.com` → Cloudflare Access sign-in →
Cloudflare Tunnel → **RavenCorvus add-on** on the farmhouse HAOS box → Home Assistant.

**Cost:** $0. Cloudflare Zero Trust Free covers 50 users. No Nabu Casa dependency.
**Time:** about 45 minutes.

Two keys protect the server:
- **Cloudflare Access** is the front door for Claude. You sign in with your email, and Access issues
  Claude an OAuth token. No secret ever appears in the URL.
- **RavenCorvus checks the Access token itself** and also accepts its own bearer key for the CLI and
  Claude Code. Anything else gets a 401.

---

## 1. Dedicated HA user and token

- [ ] HA → Settings → People → **Add person** `RavenCorvus`. Allow login: **on**. Administrator: **off**.
- [ ] Log in as that user once (private window), then Profile → Security → **Long-lived access tokens** →
      create `ravencorvus`. Copy it. This is `ha_token`.
- [ ] Generate the API key on any PC:
  ```powershell
  python -c "import secrets;print(secrets.token_urlsafe(32))"
  ```
  This is `api_key`. Store both in your password manager.

## 2. Build and install the add-on

The repo is private, so HA can't pull it as an app repository. Install it as a **local** app instead.

- [ ] HA → Settings → Apps → App store → install **Samba share**, set a password, and start it.
- [ ] On the laptop, from the repo root:
  ```powershell
  .\scripts\build-addon.ps1
  ```
- [ ] Copy `dist\ravencorvus` to `\\homeassistant.local\addons\ravencorvus`.
- [ ] HA → Settings → Apps → App store → ⋮ → **Check for updates**. Then **Local apps → RavenCorvus → Install**.
      The first build takes a few minutes on the i5.
- [ ] **Configuration** tab:
  | Option | Value |
  |---|---|
  | `ha_token` | token from step 1 |
  | `api_key` | key from step 1 |
  | `access_team` / `access_aud` | fill these in at step 4 |
  | `allowed_hosts` | `mcp.ravenswoodfarmstead.com` |
  | `allow_sensitive_domains` | leave empty for now |
- [ ] Start the add-on, turn on **Watchdog**, and check the **Log** tab:
  `ha-mcp: auth = bearer key`.

**Upgrading later:** rebuild, copy over the folder, bump `version` in `addon/ravencorvus/config.yaml`,
then Check for updates and Update.

## 3. Cloudflare Tunnel

- [ ] Apps → ⋮ → Repositories → add `https://github.com/brenner-tobias/addon-cloudflared`, then install **Cloudflared**.
- [ ] Configuration:
  ```yaml
  external_hostname: ""
  additional_hosts:
    - hostname: mcp.ravenswoodfarmstead.com
      service: http://local-ravencorvus:8765
  ```
- [ ] Start it and complete the Cloudflare login link in its log.
- [ ] Cloudflare → Zero Trust → Networks → Tunnels shows the tunnel as **Healthy**, and DNS has a proxied `mcp` CNAME.

## 4. Cloudflare Access (do this before connecting Claude)

- [ ] Zero Trust → Access → Applications → **Add an application** → **Self-hosted**.
  - Domain: `mcp.ravenswoodfarmstead.com`
  - Policy: **Allow**, Include → Emails → your address
  - Login method: One-time PIN (or Google)
  - Turn on **Managed OAuth** for MCP clients, if your dashboard shows it. This is what lets Claude sign in.
    Cloudflare added it in 2025, so the exact label may differ.
- [ ] Open the app's **Overview** and copy the **Application Audience (AUD) tag**.
- [ ] Your team name is in Zero Trust → Settings → Custom pages (`<team>.cloudflareaccess.com`).
- [ ] Back in the RavenCorvus add-on, set `access_team` and `access_aud`, then restart. The log should read:
  `ha-mcp: auth = Cloudflare Access (https://<team>.cloudflareaccess.com), bearer key`

### Firewall rule: only Anthropic gets through

Anthropic's connector traffic comes from `160.79.104.0/21`.

- [ ] Cloudflare → ravenswoodfarmstead.com → Security → WAF → Custom rules → **Create rule**
  - Name: `RavenCorvus - Anthropic only`
  - Expression:
    ```
    (http.host eq "mcp.ravenswoodfarmstead.com" and not ip.src in {160.79.104.0/21})
    ```
  - Action: **Block**
- If you also use the CLI from home over the tunnel, add your home IP to the set, or reach the add-on on
  the LAN instead.

## 5. Connect Claude

- [ ] claude.ai → Settings → Connectors → **Add custom connector**
  - Name: `RavenCorvus`
  - URL: `https://mcp.ravenswoodfarmstead.com/mcp`
- [ ] Claude opens the Cloudflare Access sign-in. Enter your email and the PIN.
- [ ] In the connector's tool list, set `call_service`, `set_state`, `fire_event` and `create_backup` to
      **Ask every time**. Leave the read tools on auto.
- [ ] Smoke test:
  1. "Check my Home Assistant status."
  2. "List my lock and alarm entities."
  3. "Show the last trace for the night path automation."

### If Claude can't complete the Access sign-in

Don't loosen Access. Instead, either:
- Turn on `path_key` in the add-on and use `https://mcp.ravenswoodfarmstead.com/<api_key>/mcp`, keeping
  the Anthropic-only WAF rule. Rotate the key if it ever appears anywhere. Or:
- Wait for request-header auth (static bearer) to reach your account. RavenCorvus already accepts
  `Authorization: Bearer <api_key>`.

## 6. Claude Code / CLI (optional)

```powershell
$env:HASS_URL="http://homeassistant.local:8123"; $env:HASS_TOKEN="<ha_token>"
uv run ha status
uv run ha traces automation.night_path
```
or point Claude Code at the remote server:
```powershell
claude mcp add --transport http ravencorvus https://mcp.ravenswoodfarmstead.com/mcp --header "Authorization: Bearer <api_key>"
```
(This needs an Access **service token** policy or your IP in the WAF rule, because it can't do the email PIN.)

## Turning on locks and the alarm later

Sensitive domains stay blocked until you opt in. When the Z-Wave locks and the Ring keypad are paired:
- `allow_sensitive_domains`: `lock`, `alarm_control_panel`
- `denied_entities`: anything that must never be touched, e.g. `lock.well_house`

Keep the Claude connector's write tools on **Ask every time**.

## Troubleshooting

| Symptom | Check |
|---|---|
| Add-on won't start, log says `ha_token is required` | Fill in Configuration and save. |
| `401` in the add-on log for every call | The token was made by the wrong user, or it expired. Regenerate it in HA. |
| Tunnel Healthy, but 502 at the hostname | `service:` must be `http://local-ravencorvus:8765`, and the add-on must be running. |
| Claude says "Couldn't reach the MCP server" | Check the WAF rule, that Access Managed OAuth is on, and the tunnel. |
| Requests reach the add-on but get 401 | `access_team`/`access_aud` don't match the Access app. Copy the AUD again. |
| `421`/`400` responses | `allowed_hosts` doesn't match the hostname. |
| Traces say "no 'id' attribute" | That automation is YAML-only without an `id:`. Add one and reload. |

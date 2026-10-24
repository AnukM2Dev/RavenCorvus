# RavenCorvus remote MCP setup

Goal: Claude (web, desktop, iPhone, iPad) → `mcp.ravenswoodfarmstead.com` → Cloudflare Tunnel → ha-mcp app on the farmhouse HAOS box.

Cost: $0. Cloudflare Zero Trust Free covers up to 50 users. No Nabu Casa dependency for this path.

## 1. ha-mcp app

- [ ] Settings → Apps → ⋮ → Repositories → add `https://github.com/homeassistant-ai/ha-mcp`
- [ ] Install **Home Assistant MCP Server**. Turn on Start on boot and Watchdog, then start it.
- [ ] Create a dedicated non-admin HA user `ravencorvus` if the app supports a token from a separate user.
- [ ] Profile → Security → Long-lived access tokens → create `ravencorvus-mcp` and paste it into the app config.
- [ ] Check the app **Log** and write down the port and MCP path: `http://<host>:____/____`
- [ ] Local test from a PC on the farmhouse LAN:
  ```powershell
  curl.exe -i http://homeassistant.local:<port>/<path>
  ```
  Any MCP or HTTP response other than connection refused means it's alive.

## 2. Cloudflare Tunnel

- [ ] Apps → Repositories → add `https://github.com/brenner-tobias/addon-cloudflared`
- [ ] Install **Cloudflared**.
- [ ] Config:
  ```yaml
  external_hostname: ""        # leave blank unless you also want HA itself exposed
  additional_hosts:
    - hostname: mcp.ravenswoodfarmstead.com
      service: http://localhost:<ha-mcp-port>
  ```
- [ ] Start it, open the log, and complete the Cloudflare login link it prints.
- [ ] Cloudflare dashboard → Zero Trust → Networks → Tunnels → confirm the tunnel shows **Healthy**.
- [ ] DNS → confirm the `mcp` CNAME exists and is proxied (orange cloud).

## 3. Lock it down. Do this before step 4.

The connector can unlock doors and disarm the alarm.

- [ ] Zero Trust → Access → Applications → **Add** → Self-hosted
  - Domain: `mcp.ravenswoodfarmstead.com`
  - Session: 24h
- [ ] Policy **Allow**, Include → Emails → your address only.
- [ ] Test: open `https://mcp.ravenswoodfarmstead.com/<path>` in a private window. It should show the Cloudflare login page.

**If Claude can't get past Access in step 4**, which is likely because the connector can't do an interactive login:
- Option A: a ha-mcp **Webhook Proxy** secret URL (a long random path) instead of Access, plus a WAF rate limit on the hostname.
- Option B: an Access **Service Auth** policy with a service token. This only works if the connector lets you add custom headers.
- Use one of these, never an unprotected hostname.

## 4. Claude connector

- [ ] claude.ai → Settings → Connectors → **Add custom connector**
  - Name: `RavenCorvus`
  - URL: `https://mcp.ravenswoodfarmstead.com/<path>`
- [ ] Open the connector's tool list and set lock unlock/open, alarm disarm, and camera-off tools to **Ask every time**.
- [ ] Smoke test:
  1. "Can you see my Home Assistant?"
  2. "List my lock and alarm entities."
  3. "Show the last trace for the PANIC automation."

## 5. Skill

- [ ] Save the **ravencorvus** skill (proposed in chat, also in `skills/ravencorvus/SKILL.md`).
- [ ] Commit this repo's `skills/` and `docs/` to `AnukM2Dev/RavenCorvus`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Tunnel shows Down | Cloudflared app log. Re-run the login link. |
| 502 at the hostname | Wrong port in `service:`. Is ha-mcp running? |
| Connector "couldn't connect" | Access is blocking it. Switch to option A or B. |
| Tools list empty | Token is wrong or expired. Regenerate it in HA. |
| Works on desktop, not phone | Re-add the connector on claude.ai (it syncs), then fully restart the app. |

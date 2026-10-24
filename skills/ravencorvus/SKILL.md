---
name: ravencorvus
description: Use when working on the farmhouse Home Assistant (RavenCorvus) — automations, dashboards, Frigate, alarm/PANIC, locks, lights, or debugging traces via the HA MCP connector or config repo.
---

# RavenCorvus — Farmhouse Home Assistant

Home Assistant OS on an HP desktop (i5-7500, 8 GB) at the farmhouse. Everything on that box runs as an HA app (add-on) — never a Windows service.
Live access: the **RavenCorvus** MCP connector at `https://rc.ravenswoodfarmstead.com/mcp` (the RavenCorvus add-on with its built-in Cloudflare tunnel, behind Access). Locally: the `ha` CLI. The add-on runs **read-only** until Mike turns writes on.
Config history: `github.com/AnukM2Dev/RavenCorvus` (private).

## Tool split

- **MCP connector** = live truth. Use it for entity/state lookup, service calls, reading traces, and testing.
- **Config repo / file edits** = durable change. Write YAML here, then commit.
- Never assume entity IDs. Look them up over MCP first, every time.

## Change workflow

1. **Discover**: list the relevant entities/areas over MCP and confirm IDs and current state.
2. **Plan**: state the trigger → conditions → actions in one short block before writing YAML.
3. **Write**: produce the automation/script/dashboard YAML. Use `mode:` explicitly and give it a clear `alias` and `id`.
4. **Validate**: run `check_config` (CLI: `ha check-config`) before any reload. Stop if `result` is `invalid`.
5. **Apply**: reload the specific domain (automation/script/template), not a full restart, unless required.
6. **Test**: trigger it (or simulate the state), then `list_automation_traces` → `get_automation_trace`. Do not call it done until the trace shows the expected path.
7. **Commit**: commit to the repo with a message that names what changed and why.

## Hard safety rules

- `lock.unlock`, `lock.open`, `alarm_control_panel.alarm_disarm`, and anything that turns cameras **off** require explicit per-call confirmation from Mike. Never batch them or auto-run them.
- Never disable the PANIC automation, Frigate recording on alarm, or the alarm panel without being asked.
- Run `create_backup` before bulk edits (renames across many entities, dashboard rewrites, integration removal).
- If a call comes back `BLOCKED` (or write tools are missing because the add-on is read-only), say which setting would allow it. Never route around the guard via scripts or events.
- Debug from the trace first (`get_automation_trace`, then `get_automation_config`). Don't rewrite an automation that a trace could explain.
- Use `call_service` with `dry_run=true` when unsure whether a call is allowed or which entities it hits.

## House conventions (current)

**Rooms**: Kitchen, Living Room, Family Room (formerly "Mike's Lounge": couch/TV on one side, games/dining on the other), Master Bedroom, Master Bath, Hallway, Laundry.

**Alarm / PANIC**
- PANIC = someone entered and did not disarm. Play the biohazard alarm mp3, all cameras record until PANIC is cleared, and all lights go to max brightness.
- During PANIC, the Family Room Vizio (ONN box on HDMI-1) shows Frigate feeds, **video only**. AirCast audio routes through that TV.
- When home, the front door opening should trigger an audible chime on the Master Show Echo, not a push notification.
- Alarm events make Frigate record for 40 minutes.
- Alerts go to the HA app for Mike (iPhone + iPad) and Kevin (iPhone).
- The wall tablet (Lenovo TB-X606F, between the Kitchen and the Living Room) auto-pops the alarm panel on PANIC and dims overnight but never turns off.

**Frigate**
- Cameras: kitchen, Living Room, Family Room, Hallway.
- Motion recording runs 2 minutes per event. Filter out bugs and dust.
- Profiles:
  - *Armed – Nightwatch*: all cameras on and recording.
  - *Disarmed – Home*: all cameras on for viewing, **no recording**.
  - *Privacy Override*: all cameras off.
- Face and plate recognition are not a priority.

**Lights**
- Bedtime lights-off covers the Family Room, kitchen, laundry, bedroom, and bathroom. **Not the Living Room.**
- Grow lights (Corner Spot Lamp and Palm light, both in the Living Room) run on at 05:00 and off at 19:00.
- The Living Room "Bookshelf Multi" Gosund strip powers a camera from its USB port. **Never turn that USB port off.**
- The night path is a motion sensor at the bedroom exit that turns on the hallway, bathroom, and kitchen lights.

**Expected-unavailable**: the Kasa plugs are in storage, so ignore their unavailable entities.

**Maintenance reminders** (surface on the wall tablet)
- Trash: out Tuesday night, can in Wednesday.
- HVAC filter: every 4 weeks.

## Hardware notes

- Zigbee: SONOFF Dongle Lite MG21 (firmware 8.0.2).
- Z-Wave: Sonoff Dongle-PZG23 (800 series). A Ring keypad is planned to pair with it.
- Locks: Veise VE017 via the G1 gateway (KK Home cloud). The Foxgard lock is retired. Mike is moving to local Z-Wave locks.
- Router: a TP-Link Archer BE230 is planned behind the Calix GigaSpire in bridge mode.
- HACS is installed.

## Output style

Lead with the change. Give copy-paste-ready YAML and commands. Flag anything that needs a paid service or a new integration upfront.

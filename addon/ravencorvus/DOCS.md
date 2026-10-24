# RavenCorvus

Serves the RavenCorvus MCP tools over streamable HTTP at `http://local-ravencorvus:8765/mcp`
on the add-on network. Expose it with the Cloudflared app and protect it with Cloudflare Access.
See `docs/SETUP.md` in the repository for the full walkthrough.

Default posture: sensitive domains are blocked
until you list them under **Allow sensitive domains**.

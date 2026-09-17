# AI LaunchKit documentation

Use code for current service/config facts and these documents for focused
procedures.

## Maintained guides

| Guide | Purpose |
| --- | --- |
| [../README.md](../README.md) | Operator overview and source-of-truth boundaries |
| [../AGENTS.md](../AGENTS.md) | Agent and change rules |
| [../ADDING_NEW_SERVICE.md](../ADDING_NEW_SERVICE.md) | Add or integrate a service |
| [../monitoring/README.md](../monitoring/README.md) | External n8n queue watchdog and alerting |
| [CALCOM_SETUP.md](CALCOM_SETUP.md) | Cal.com setup and calendar integration |
| [../cloudflare-instructions.md](../cloudflare-instructions.md) | Cloudflare Tunnel design and safety |
| [../vexa-troubleshooting-workarounds.md](../vexa-troubleshooting-workarounds.md) | Vexa lifecycle and troubleshooting |
| [N8N-MCP authentication](n8n-mcp-auth.md) | Public API JWT and inbound MCP token contract |
| [langfuse-redis.md](langfuse-redis.md) | Langfuse Redis connection and rollback contract |

## Authoritative implementation sources

- `../docker-compose.yml` — services, profiles, dependencies, health checks,
  networks, volumes, and ports.
- `../scripts/install.sh` plus numbered scripts — install lifecycle.
- `../scripts/04_wizard.sh` — selectable profiles.
- `../.env.example` — configuration names only; never inspect real `.env`.
- Caddy configuration under the repository's Caddy paths — public routing.

## Historical context

[`../memory-bank/README.md`](../memory-bank/README.md) indexes durable design
context, archived features, and retrospectives. Historical files are evidence,
not current operational instructions.

The previous `README_Services.md` and `n8n-installer-developer-guide.md` were
removed because they duplicated Compose/scripts, exceeded practical review
size, and drifted from the fork. Git history retains them when archaeology is
needed.

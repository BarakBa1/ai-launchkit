# AI LaunchKit documentation

Use code for current service/config facts and these documents for focused
procedures.

## Maintained guides

| Guide | Purpose |
| --- | --- |
| [../README.md](../README.md) | Operator overview and source-of-truth boundaries |
| [../AGENTS.md](../AGENTS.md) | Agent and change rules |
| [../ADDING_NEW_SERVICE.md](../ADDING_NEW_SERVICE.md) | Add or integrate a service |
| [../ai4trade/ops/BACKUP.md](../ai4trade/ops/BACKUP.md) | AI4TRADE backups: coverage, the restore test, VPS steps |
| [../ai4trade/ops/RECONCILE_CADDY.md](../ai4trade/ops/RECONCILE_CADDY.md) | Reconcile the AI4TRADE dashboard Caddy addon with the repo |
| [../ai4trade/ops/MINIFLUX_RETENTION.md](../ai4trade/ops/MINIFLUX_RETENTION.md) | Miniflux read-entry retention (90 days) |
| [../monitoring/README.md](../monitoring/README.md) | External n8n queue watchdog (legacy n8n) |
| [CALCOM_SETUP.md](CALCOM_SETUP.md) | Cal.com setup and calendar integration |
| [../cloudflare-instructions.md](../cloudflare-instructions.md) | Cloudflare Tunnel design and safety |
| [../vexa-troubleshooting-workarounds.md](../vexa-troubleshooting-workarounds.md) | Vexa lifecycle and troubleshooting |
| [N8N-MCP authentication](n8n-mcp-auth.md) | Public API JWT and inbound MCP token contract (legacy n8n) |

## Authoritative implementation sources

- `../docker-compose.yml` — services, profiles, dependencies, health checks,
  networks, volumes, and ports.
- `../ai4trade/compose.yml` and `../ai4trade/caddy-ai4trade.conf` — the
  AI4TRADE app stack and its dashboard route. The app's own docs are in
  `../../ai-n8n-trading/docs/` (start at `README.md` there).
- `../scripts/install.sh` plus numbered scripts — install lifecycle.
- `../scripts/04_wizard.sh` — selectable profiles.
- `../.env.example` — configuration names only; never inspect real `.env`.
- Caddy configuration under the repository's Caddy paths — public routing.

## Repository-local state

- `../temp/` is the ignored repository-owned root for transient and quarantine
  material; quarantine manifests include restore metadata.
- `../worktrees/` is the ignored repository-owned root for auxiliary Git
  worktrees.
- Shared agent rules and skills live in the workspace repo at `../../.claude/`
  (`rules/RULES.md`); do not put scratch there.

## Historical context

[`../memory-bank/README.md`](../memory-bank/README.md) indexes the original
project brief and design context. It is history, not current instructions.

The previous `README_Services.md` and `n8n-installer-developer-guide.md` were
removed because they duplicated Compose/scripts, exceeded practical review
size, and drifted from the fork. Git history retains them when archaeology is
needed.

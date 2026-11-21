# AI LaunchKit — operator fork

Docker Compose-based source for the VPS that hosts AI4TRADE: the `ai4trade`
app (Alpaca paper) and the services it uses.
This checkout tracks the operator-owned
[`BarakBa1/ai-launchkit`](https://github.com/BarakBa1/ai-launchkit) fork; upstream
lineage is
[`freddy-schuetz/ai-launchkit`](https://github.com/freddy-schuetz/ai-launchkit).

Local files describe intended infrastructure. Confirm running containers,
versions, profiles, routes, and health on the host before making an operational
claim.

## Start here

| Need | Source |
| --- | --- |
| Agent/change rules | [AGENTS.md](AGENTS.md) |
| Documentation map | [docs/README.md](docs/README.md) |
| Services, dependencies, profiles | [docker-compose.yml](docker-compose.yml) |
| Installation sequence | [scripts/install.sh](scripts/install.sh) and numbered scripts under `scripts/` |
| Add a service | [ADDING_NEW_SERVICE.md](ADDING_NEW_SERVICE.md) |
| Environment template | [.env.example](.env.example) — never open a real `.env` in agent work |
| AI4TRADE app stack | [ai4trade/](#the-ai4trade-app-stack) here; the app itself is [../ai-n8n-trading/](../ai-n8n-trading/docs/infrastructure/README.md) |

## What this repository controls

- Docker Compose service definitions, profiles, networks, volumes, and health
  checks.
- Caddy routing templates.
- Ubuntu installation, update, cleanup, and service-selection scripts.
- Optional setup/init helpers for services that need extra lifecycle work.

It does not contain the AI4TRADE app code, its config and prompts, its
database schema or broker logic. Those live in `../ai-n8n-trading/`.

## The ai4trade app stack

The app runs as its own Compose project, defined here and deployed by the app
repo's `scripts/deploy.sh`:

| File | On the VPS | What it is |
| --- | --- | --- |
| [ai4trade/compose.yml](ai4trade/compose.yml) | `/root/ai4trade/compose.yml` | `ai4trade-main` and `ai4trade-guard`, one image `ai4trade:<git sha>`, `stop_grace_period: 60s`, joined to the launchkit network |
| [ai4trade/caddy-ai4trade.conf](ai4trade/caddy-ai4trade.conf) | `/root/ai-launchkit/caddy-addon/ai4trade.conf` | The dashboard route: Caddy basic auth, then the app's own sign-in; `/metrics` not public |
| [ai4trade/ops/](ai4trade/ops/) | `/root/ops/` | Nightly `pg_dump`, the restore test, and runbooks: [backups](ai4trade/ops/BACKUP.md), [Caddy reconcile](ai4trade/ops/RECONCILE_CADDY.md), [Miniflux retention](ai4trade/ops/MINIFLUX_RETENTION.md) |

The app uses these launchkit services: PostgreSQL (`ai4trade` and `barak`
schemas), Miniflux, SearXNG, Crawl4AI, Weaviate and Caddy. n8n is legacy: it
only ingests news until the app repo's plan stage B2 retires it.


## Install on a supported host

Run on the intended Ubuntu host, not from the Windows documentation checkout:

```bash
git clone https://github.com/BarakBa1/ai-launchkit.git
cd ai-launchkit
sudo bash ./scripts/install.sh
```

The installer orchestrates the numbered scripts. Do not run a middle step in
isolation unless its prerequisites and recovery path are understood.

## Inspect and operate

```bash
docker compose config --profiles
docker compose config --quiet
docker compose ps
docker compose logs --tail=200 <service>
sudo bash ./scripts/update.sh
sudo bash ./scripts/docker_cleanup.sh
```

Profiles and service names change over time; derive the current list from
`docker-compose.yml` and `scripts/04_wizard.sh` instead of maintaining a second
manual encyclopedia.

## Configuration and secrets

- `scripts/03_generate_secrets.sh` creates the runtime `.env` from the tracked
  template.
- Never commit, display, log, or document real `.env` values.
- `N8N_MCP_TOKEN` and `N8N_API_KEY` are separate credentials. The former is the
  generated inbound n8n-MCP token. If the `n8n-mcp` profile is selected, the
  latter must be a public API JWT issued by n8n under Settings -> API; the
  generator, wizard, and service runner reject missing or malformed values.
  The installer starts core n8n first, validates the key through the bounded
  public `N8N_URL` preflight, and only then launches the full selected stack;
  the n8n-MCP entrypoint repeats an authoritative check against healthy n8n
  before starting the original MCP command. A blank `N8N_API_KEY` is valid when
  that profile is unused.
- Prefer service-to-service Docker networking. Expose only required public
  routes through Caddy or an explicitly designed Cloudflare Tunnel.
- A Cloudflare Tunnel can bypass Caddy protections; read
  [cloudflare-instructions.md](cloudflare-instructions.md) before enabling it.

## Change checklist

1. Read [AGENTS.md](AGENTS.md) and identify affected profiles/dependencies.
2. Update Compose, wizard, environment template, Caddy, and lifecycle scripts
   together when the service contract spans them.
3. Add a health check, persistent storage, resource expectations, and rollback.
4. Run shell syntax checks and `docker compose config --quiet`.
5. Test the smallest profile set, then dependent profiles.
6. Update [docs/README.md](docs/README.md) and relevant runbooks.
7. Treat production deployment as a separate explicitly authorized operation;
   capture current state and a rollback point first.

## License

[Apache 2.0](LICENSE), preserved from upstream.

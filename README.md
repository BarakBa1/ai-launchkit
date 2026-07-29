# AI LaunchKit — operator fork

Docker Compose-based source for the VPS that hosts AI4TRADE infrastructure.
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
| AI4TRADE relationship | [../ai-n8n-trading/docs/infrastructure/ai-launchkit.md](../ai-n8n-trading/docs/infrastructure/ai-launchkit.md) |

## What this repository controls

- Docker Compose service definitions, profiles, networks, volumes, and health
  checks.
- Caddy routing templates.
- Ubuntu installation, update, cleanup, and service-selection scripts.
- Optional setup/init helpers for services that need extra lifecycle work.

It does not contain AI4TRADE workflows, prompts, database application schema,
or broker logic. Those live in `../ai-n8n-trading/`.

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

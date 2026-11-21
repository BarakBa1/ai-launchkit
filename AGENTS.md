# AGENTS.md — AI LaunchKit

Repository-specific guidance for the operator-owned infrastructure fork. Read
[`C:\GitHub\AGENTS.md`](../AGENTS.md) first.

## What runs on this host

AI4TRADE is the `ai4trade` Python app (Alpaca paper), defined by `ai4trade/compose.yml` here and deployed by `../ai-n8n-trading/scripts/deploy.sh`. It uses the launchkit PostgreSQL, Miniflux, SearXNG, Crawl4AI, Weaviate and Caddy. n8n is legacy: it only ingests news until the app repo's plan stage B2 retires it.

## n8n version requirement — n8n 1.* only

While n8n runs, the `n8n` profile must run **n8n 1.*** with Pyodide-based Python Code nodes. The news-ingest workflows are not compatible with n8n 2.* (which switches to the native task runner / `typeVersion: 3` Code node and drops Pyodide). Keep the n8n image pinned to `n8nio/n8n:1` (`n8n/Dockerfile`, built by the `n8n` service in `docker-compose.yml`; not `:latest`, not `:2`); keep `N8N_NATIVE_PYTHON_RUNNER: false`. An n8n 1.* to 2.* upgrade is **not** planned — see [`../ai-n8n-trading/docs/legacy-n8n.md`](../ai-n8n-trading/docs/legacy-n8n.md).

## Authority and state boundaries

- `origin` is `BarakBa1/ai-launchkit`; `freddy-schuetz/ai-launchkit` is upstream
  lineage.
- Local Compose/scripts are the intended-state source. They do not prove what
  currently runs on the VPS.
- Production host reads/writes require exact user authorization. Any production
  n8n VPS modification also follows workspace rule V1.
- The AI4TRADE app (code, `config/`, prompts, database schema and migrations, the deploy script) belongs to `../ai-n8n-trading/`. This repo owns the host shape, including `ai4trade/compose.yml`, the Caddy addon and the backup scripts in `ai4trade/ops/`.
- Production VPS changes also follow the current operating mode (workspace rule M23) and paper V1 for app deploys.

## Documentation map

- [README.md](README.md) — concise operator overview.
- [docs/README.md](docs/README.md) — maintained runbooks and historical context.
- [ADDING_NEW_SERVICE.md](ADDING_NEW_SERVICE.md) — service-integration checklist.
- [docker-compose.yml](docker-compose.yml) — authoritative services, profiles,
  dependencies, ports, volumes, and health checks.
- `scripts/` — authoritative install/update lifecycle.

Do not infer current behavior from the removed generated service encyclopedia
or completed memory-bank task journals; use code and current runbooks.

## Repository-local layout

Scratch goes in `temp/` and auxiliary Git worktrees in `worktrees/` (workspace rule M10). Keep folder names lowercase kebab-case, and preserve service-specific paths.

## Safety

- Never read or display `.env` values. Use `.env.example` for variable names.
- Never commit credentials, tokens, certificates, private keys, generated
  runtime directories, backups, or dependency output.
- Before destructive Docker/volume operations, identify exact targets, capture
  backups, explain data loss, and obtain explicit approval.
- Do not add dependencies without user approval.
- Preserve unrelated working-tree changes.

## Architecture

Installation is orchestrated by `scripts/install.sh` and the numbered scripts:

1. system preparation;
2. Docker installation;
3. secret/environment generation;
4. profile selection and optional setup helpers;
5. service startup and optional initialization helpers;
6. final report.

Compose profiles select optional services. Shared patterns include Caddy
routing, named volumes, health checks, explicit dependencies, and internal
Docker DNS. Vexa is an external cloned Compose project managed by
`04a_setup_vexa.sh` and `05a_init_vexa.sh`, not a service in the main Compose
file.

## Change workflow

1. Read the current Compose service and adjacent analogous service.
2. Map required changes across `docker-compose.yml`, `.env.example`,
   `scripts/04_wizard.sh`, Caddy configuration, setup/init scripts, and docs.
3. Reuse `scripts/utils.sh` logging and existing idempotency patterns.
4. Define profiles, dependencies, health checks, volumes, resource needs,
   public exposure, backup, and rollback.
5. Validate on Linux:

   ```bash
   bash -n scripts/*.sh
   docker compose config --quiet
   docker compose config --profiles
   ```

6. Test the smallest affected profile set and inspect health/logs.
7. Update [docs/README.md](docs/README.md) and focused runbooks. Do not duplicate
   the complete service catalog in prose.

## Operations

```bash
sudo bash ./scripts/install.sh
sudo bash ./scripts/update.sh
sudo bash ./scripts/apply_update.sh
sudo bash ./scripts/docker_cleanup.sh
docker compose ps
docker compose logs --tail=200 <service>
```

`docker compose down -v`, volume removal, reset/reinstall, firewall changes, and
production restarts are destructive or availability-affecting and require
explicit scope/approval.

## Git

Follow workspace rule G1: ask before committing or pushing here unless the current request asks for it.

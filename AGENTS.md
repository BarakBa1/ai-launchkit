# AGENTS.md — AI LaunchKit

Repository-specific guidance for the operator-owned infrastructure fork. Read
[`C:\GitHub\AGENTS.md`](../AGENTS.md) first.

## Authority and state boundaries

- `origin` is `BarakBa1/ai-launchkit`; `freddy-schuetz/ai-launchkit` is upstream
  lineage.
- Local Compose/scripts are the intended-state source. They do not prove what
  currently runs on the VPS.
- Production host reads/writes require exact user authorization. Any production
  n8n VPS modification also follows workspace rule V1.
- AI4TRADE workflows and prompts belong to `../ai-n8n-trading/`, not this repo.

## Documentation map

- [README.md](README.md) — concise operator overview.
- [docs/README.md](docs/README.md) — maintained runbooks and historical context.
- [ADDING_NEW_SERVICE.md](ADDING_NEW_SERVICE.md) — service-integration checklist.
- [docker-compose.yml](docker-compose.yml) — authoritative services, profiles,
  dependencies, ports, volumes, and health checks.
- `scripts/` — authoritative install/update lifecycle.

Do not infer current behavior from the removed generated service encyclopedia
or completed memory-bank task journals; use code and current runbooks.

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

Follow workspace Git safety. Never commit/push automatically, force-push a
protected branch, or run `git reset --hard` without explicit confirmation.

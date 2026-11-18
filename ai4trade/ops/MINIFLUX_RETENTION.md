# Miniflux read-entry retention: 90 days (ruling E7)

Repo change: root `docker-compose.yml`, `miniflux` service. `CLEANUP_ARCHIVE_READ_DAYS` goes from `14` to `90`. `CLEANUP_ARCHIVE_UNREAD_DAYS=30` and `CLEANUP_BATCH_SIZE=10000` are unchanged.

Status: the repo change is made and `docker compose config --quiet` passes (exit 0, with warnings only for unset variables, as expected without `.env`). NOT applied on the VPS.

## Observed state (read-only, 2026-11-18 08:13 UTC)

- VPS `/root/ai-launchkit/docker-compose.yml`, lines 720 to 722: `CLEANUP_ARCHIVE_READ_DAYS=14`, `CLEANUP_ARCHIVE_UNREAD_DAYS=30`, `CLEANUP_BATCH_SIZE=10000`.
- Container `miniflux` (`miniflux/miniflux:latest`): running, healthy, StartedAt 2026-09-14T23:07:43Z.
- The effective value inside the running container is NOT verified. Reading its environment is not permitted here, so the readback below checks the compose intent and the recreate, not the container's environment.
- Miniflux database size: 2166 MB (read-only `pg_database_size`). It is not part of the nightly logical dump (see `ai4trade/ops/BACKUP.md`, G4).

## Apply on the VPS (orchestrator, after approval)

Recreating `miniflux` interrupts the Miniflux web UI and feed polling for a few seconds. Feed data stays in the `miniflux` database.

```bash
# 1. backup
cp -p /root/ai-launchkit/docker-compose.yml /root/backups/miniflux-retention-$(date -u +%Y%m%dT%H%M%SZ).yml

# 2. precheck: must print 1
grep -c '^      - CLEANUP_ARCHIVE_READ_DAYS=14$' /root/ai-launchkit/docker-compose.yml

# 3. edit (only that line)
sed -i 's/^      - CLEANUP_ARCHIVE_READ_DAYS=14$/      - CLEANUP_ARCHIVE_READ_DAYS=90/' /root/ai-launchkit/docker-compose.yml

# 4. validate
cd /root/ai-launchkit && docker compose config --quiet && echo compose_valid

# 5. apply (approval needed)
docker compose --profile miniflux up -d --no-deps miniflux
```

Do not copy the whole repo `docker-compose.yml` to the VPS. The VPS file differs from the repo in other places, and a full copy would change other services.

## Readback

```bash
docker inspect miniflux --format '{{.State.Status}} {{.State.Health.Status}} started={{.State.StartedAt}}'   # running healthy, StartedAt is new
cd /root/ai-launchkit && docker compose --profile miniflux config --format json | python3 -c 'import json,sys; e=json.load(sys.stdin)["services"]["miniflux"]["environment"]; print(e.get("CLEANUP_ARCHIVE_READ_DAYS"), e.get("CLEANUP_ARCHIVE_UNREAD_DAYS"))'   # prints: 90 30
```

The last command prints only the two retention values. Compose's view of the file is the intent, not proof of the running container.

## Rollback

```bash
cp -p /root/backups/miniflux-retention-<stamp>.yml /root/ai-launchkit/docker-compose.yml
cd /root/ai-launchkit && docker compose --profile miniflux up -d --no-deps miniflux
```

## Expected effect

Read entries stay about 76 days longer before the cleanup archives them, so the `miniflux` database grows. Its size was 2166 MB at read time. The nightly cleanup runs on Miniflux's own schedule.

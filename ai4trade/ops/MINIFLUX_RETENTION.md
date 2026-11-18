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

## Disk budget

The growth is not measured yet. These are the bounds and the way to measure them.

- Current size: about 2.1 GB (2166 MB, `pg_database_size`, read-only, 2026-11-18). This is the baseline for the comparison below.
- Planning figure (estimate, not measured): if the current 2.1 GB were almost all read entries at a flat daily volume, 90 days of read retention would reach about 2.1 x 90 / 14, roughly 13.5 GB, so about 11 GB more. Unread entries stay as they are, and indexes grow with the rows, so the real figure can be above or below this. Plan with it until the measurement below replaces it.
- Disk context (BACKUP.md G6): at read time `/` had 64 GB available, and the local nightly dumps alone reach about 58 GB at 14-day retention. The extra Miniflux growth comes out of the same headroom. Re-read `df -h /` before applying.
- The `miniflux` database is in the file-level kopia snapshot, not in the logical dump (G4). Its growth also increases the snapshot and off-site storage.

How to measure (read-only, on the VPS; the shared Postgres container is `postgres`):

```bash
docker exec postgres psql -U postgres -d miniflux -At -c "select pg_size_pretty(pg_database_size('miniflux'));"
docker exec postgres psql -U postgres -d miniflux -At -c "select count(*), pg_size_pretty(sum(pg_column_size(e))) from entries e where status = 'read' and changed_at >= now() - interval '14 days';"
```

The second query gives the read rows and their stored size over the last 14 days, which is the current daily read volume times 14. Growth estimate at 90 days is that size divided by 14, times 76. Check the column names against the running Miniflux schema (`\d entries`) first. After apply, record `pg_database_size` once a day and compare with the 2166 MB baseline. Growth above the budget the operator sets is the trigger for the rollback below.

Decision for the operator: the maximum size of the `miniflux` database and the minimum free space on `/` that the change may use. No number is set in this runbook. A starting proposal is a ceiling of 8 GB for the database, to be confirmed against the measured daily growth.

Rollback note: restoring 14 days (Rollback section) stops further growth and the cleanup removes read entries older than 14 days on its next run. It does not give disk back to the OS. Postgres keeps the freed pages inside the database files until `VACUUM FULL` or a rebuild, which locks the table and is an availability decision of its own.

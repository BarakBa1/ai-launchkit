# Langfuse Redis connection and rollback

The Langfuse 3.x services use the shared passwordless Valkey by default. The
Compose environment anchor supplies an explicit `REDIS_CONNECTION_STRING`, so
the Langfuse image does not enter its `REDIS_HOST` path and stringify an absent
`REDIS_AUTH` into an `AUTH` command. `langfuse-web` inherits this same anchor;
do not override it with an empty service-level value.

The tracked images are intentionally pinned to the rehearsed Langfuse 3.177.1
manifests:

- `langfuse/langfuse-worker:3.177.1@sha256:a578e58e241e3a1507214c6628d272ce806134840345373048039a088b661356`
- `langfuse/langfuse:3.177.1@sha256:2af971c857dac3da0d22e9ba5168150853a266a213d0082fed0e476f2905aabe`

For an authenticated or externally hosted Redis, set a non-empty
`REDIS_CONNECTION_STRING` explicitly. For TLS, set `REDIS_TLS_ENABLED=true` and
use `REDIS_TLS_CA_PATH`, `REDIS_TLS_CERT_PATH`, and `REDIS_TLS_KEY_PATH`. These
are the Langfuse variable names consumed by its Redis client; the legacy names
without `_PATH` are not part of this contract.

The release-pinned contract is documented by Langfuse's [3.177.1 Redis
client](https://github.com/langfuse/langfuse/blob/v3.177.1/packages/shared/src/server/redis/redis.ts)
and [environment schema](https://github.com/langfuse/langfuse/blob/v3.177.1/packages/shared/src/env.ts).

## Rollback gate

Rollback is an authorized, backup-first service change. Preserve the current
Compose file and image references before changing them, and validate health,
queue processing, and Redis command monitoring after each service operation.

Do not roll back by adding `REDIS_AUTH` alone: the shared Valkey remains
passwordless and would reject that credential. An unset or empty parent
`REDIS_CONNECTION_STRING` intentionally expands to the local passwordless URL;
it must not be used to configure an external endpoint. If the external Redis
requires authentication, restore its complete connection-string configuration
and matching Redis-side authentication as one reviewed change.

Any image rollback must restore the web and worker pair together, then rerun
the disposable passwordless-Valkey rehearsal. A production rollback also
requires the host's normal quiescence, backup, one-service-at-a-time
readback, and closure gates; this document does not authorize a live change.

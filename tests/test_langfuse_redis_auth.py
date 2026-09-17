import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from urllib.parse import urlsplit
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def compose_command():
    docker_compose = shutil.which("docker-compose")
    if docker_compose:
        return [docker_compose]

    docker = shutil.which("docker")
    if docker:
        result = subprocess.run(
            [docker, "compose", "version"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [docker, "compose"]

    raise unittest.SkipTest("Docker Compose CLI is required for Compose contract tests")


def docker_rehearsal_compose():
    return textwrap.dedent(
        """
        services:
          redis:
            image: docker.io/valkey/valkey:8-alpine
            command: ["valkey-server", "--save", "", "--appendonly", "no", "--loglevel", "warning"]
            healthcheck:
              test: ["CMD", "redis-cli", "ping"]
              interval: 1s
              timeout: 5s
              retries: 30

          redis-monitor:
            image: docker.io/valkey/valkey:8-alpine
            entrypoint: ["redis-cli"]
            command: ["-h", "redis", "-p", "6379", "MONITOR"]
            restart: always
            depends_on:
              redis:
                condition: service_healthy
            healthcheck:
              test: ["CMD-SHELL", "redis-cli -h redis -p 6379 ping"]
              interval: 1s
              timeout: 5s
              retries: 30

          postgres:
            image: docker.io/library/postgres:17-alpine
            environment:
              POSTGRES_USER: postgres
              POSTGRES_PASSWORD: rehearsal-postgres-password
              POSTGRES_DB: langfuse
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U postgres -d langfuse"]
              interval: 2s
              timeout: 5s
              retries: 30

          clickhouse:
            image: clickhouse/clickhouse-server:latest
            environment:
              CLICKHOUSE_DB: default
              CLICKHOUSE_USER: clickhouse
              CLICKHOUSE_PASSWORD: rehearsal-clickhouse-password
            healthcheck:
              test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:8123/ping"]
              interval: 2s
              timeout: 5s
              retries: 60

          minio:
            image: minio/minio:latest
            entrypoint: ["sh"]
            command: ["-c", "mkdir -p /data/langfuse && minio server --address :9000 /data"]
            environment:
              MINIO_ROOT_USER: minio
              MINIO_ROOT_PASSWORD: rehearsal-minio-password
            healthcheck:
              test: ["CMD", "mc", "ready", "local"]
              interval: 2s
              timeout: 5s
              retries: 30

          langfuse-worker:
            image: langfuse/langfuse-worker:3.177.1@sha256:a578e58e241e3a1507214c6628d272ce806134840345373048039a088b661356
            environment: &langfuse-env
              DATABASE_URL: postgresql://postgres:rehearsal-postgres-password@postgres:5432/langfuse
              SALT: rehearsal-salt
              ENCRYPTION_KEY: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
              TELEMETRY_ENABLED: "false"
              LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES: "false"
              CLICKHOUSE_MIGRATION_URL: clickhouse://clickhouse:9000
              CLICKHOUSE_URL: http://clickhouse:8123
              CLICKHOUSE_USER: clickhouse
              CLICKHOUSE_PASSWORD: rehearsal-clickhouse-password
              CLICKHOUSE_CLUSTER_ENABLED: "false"
              LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
              LANGFUSE_S3_EVENT_UPLOAD_REGION: auto
              LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: minio
              LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: rehearsal-minio-password
              LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://minio:9000
              LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
              LANGFUSE_S3_EVENT_UPLOAD_PREFIX: events/
              LANGFUSE_S3_MEDIA_UPLOAD_BUCKET: langfuse
              LANGFUSE_S3_MEDIA_UPLOAD_REGION: auto
              LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID: minio
              LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY: rehearsal-minio-password
              LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://minio:9000
              LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE: "true"
              LANGFUSE_S3_MEDIA_UPLOAD_PREFIX: media/
              LANGFUSE_S3_BATCH_EXPORT_ENABLED: "false"
              LANGFUSE_INGESTION_QUEUE_DELAY_MS: ""
              LANGFUSE_INGESTION_CLICKHOUSE_WRITE_INTERVAL_MS: ""
              REDIS_HOST: redis
              REDIS_PORT: "6379"
              REDIS_CONNECTION_STRING: redis://redis:6379
              REDIS_TLS_ENABLED: "false"
            depends_on:
              postgres:
                condition: service_healthy
              clickhouse:
                condition: service_healthy
              minio:
                condition: service_healthy
              redis:
                condition: service_healthy
              redis-monitor:
                condition: service_healthy
            healthcheck:
              test:
                - CMD-SHELL
                - >-
                  node -e "const Redis=require('ioredis'); const r=new Redis(process.env.REDIS_CONNECTION_STRING,{maxRetriesPerRequest:1});
                  r.ping().then(()=>r.quit()).catch(()=>process.exit(1))"
              interval: 5s
              timeout: 10s
              retries: 30

          langfuse-web:
            image: langfuse/langfuse:3.177.1@sha256:2af971c857dac3da0d22e9ba5168150853a266a213d0082fed0e476f2905aabe
            environment:
              <<: *langfuse-env
              NEXTAUTH_URL: http://langfuse-web:3000
              NEXTAUTH_SECRET: rehearsal-nextauth-secret
              LANGFUSE_INIT_ORG_ID: organization_id
              LANGFUSE_INIT_ORG_NAME: Rehearsal
              LANGFUSE_INIT_PROJECT_ID: project_id
              LANGFUSE_INIT_PROJECT_NAME: Rehearsal
              AUTH_DISABLE_SIGNUP: "true"
            depends_on:
              postgres:
                condition: service_healthy
              clickhouse:
                condition: service_healthy
              minio:
                condition: service_healthy
              redis:
                condition: service_healthy
              redis-monitor:
                condition: service_healthy
            healthcheck:
              test:
                - CMD-SHELL
                - >-
                  node -e "require('http').get('http://127.0.0.1:3000/api/public/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"
              interval: 5s
              timeout: 10s
              retries: 60

          queue-probe:
            image: langfuse/langfuse-worker:3.177.1@sha256:a578e58e241e3a1507214c6628d272ce806134840345373048039a088b661356
            profiles: ["probe"]
            working_dir: /app/worker
            entrypoint: ["node", "/probe.js"]
            volumes:
              - ./probe.js:/probe.js:ro
            environment:
              REDIS_CONNECTION_STRING: redis://redis:6379
            depends_on:
              redis:
                condition: service_healthy
              langfuse-web:
                condition: service_healthy
              langfuse-worker:
                condition: service_healthy
        """
    )


def docker_rehearsal_probe():
    return textwrap.dedent(
        """
        const http = require('http');
        const Redis = require('ioredis');
        const { Queue, QueueEvents, Worker } = require('bullmq');

        const url = process.env.REDIS_CONNECTION_STRING;
        const queueName = 'langfuse-redis-auth-rehearsal';
        const redis = new Redis(url, { maxRetriesPerRequest: null });
        const producer = new Redis(url, { maxRetriesPerRequest: null });
        const consumer = new Redis(url, { maxRetriesPerRequest: null });
        const eventsConnection = new Redis(url, { maxRetriesPerRequest: null });
        const queue = new Queue(queueName, { connection: producer });
        const events = new QueueEvents(queueName, { connection: eventsConnection });
        const worker = new Worker(queueName, async () => ({ ok: true }), { connection: consumer });

        function healthCheck() {
          return new Promise((resolve, reject) => {
            const request = http.get('http://langfuse-web:3000/api/public/health', response => {
              response.resume();
              if (response.statusCode === 200) resolve();
              else reject(new Error(`web health returned ${response.statusCode}`));
            });
            request.on('error', reject);
            request.setTimeout(10000, () => request.destroy(new Error('web health timeout')));
          });
        }

        async function main() {
          await Promise.all([redis.ping(), events.waitUntilReady(), worker.waitUntilReady(), healthCheck()]);
          console.log('LANGFUSE_HEALTH_OK');
          const job = await queue.add('ping', { rehearsal: true });
          const result = await job.waitUntilFinished(events, 15000);
          if (!result || result.ok !== true) throw new Error('queue job result was not successful');
          console.log('QUEUE_JOB_OK');
          await queue.obliterate({ force: true });
          await Promise.all([
            worker.close(),
            events.close(),
            queue.close(),
            redis.quit(),
            producer.quit(),
            consumer.quit(),
            eventsConnection.quit(),
          ]);
        }

        main().catch(error => {
          console.error(error.message);
          process.exitCode = 1;
        });
        """
    )


class LangfuseRedisAuthTest(unittest.TestCase):
    def test_compose_selects_authless_connection_string_in_shared_langfuse_env(self):
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        langfuse_block = compose[
            compose.index("  langfuse-worker:") : compose.index("\n  clickhouse:")
        ]

        self.assertIn(
            "REDIS_CONNECTION_STRING: ${REDIS_CONNECTION_STRING:-redis://${REDIS_HOST:-redis}:${REDIS_PORT:-6379}}",
            langfuse_block,
            "The image's REDIS_HOST path stringifies absent REDIS_AUTH; use the "
            "supported authless connection-string branch instead",
        )
        self.assertNotIn("REDIS_AUTH:", langfuse_block)

    def test_langfuse_services_do_not_send_auth_to_shared_valkey(self):
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "COMSPEC")
            if key in os.environ
        }
        environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "COMPOSE_PROFILES": "langfuse,n8n",
            }
        )

        with tempfile.TemporaryDirectory() as docker_config:
            environment["DOCKER_CONFIG"] = docker_config
            result = subprocess.run(
                compose_command()
                + [
                    "--profile",
                    "langfuse",
                    "--profile",
                    "n8n",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"Docker Compose config failed with exit code {result.returncode}",
        )
        config = json.loads(result.stdout)

        for service_name in ("langfuse-worker", "langfuse-web"):
            service_environment = config["services"][service_name].get("environment", {})
            self.assertEqual(service_environment.get("REDIS_HOST"), "redis")
            self.assertEqual(service_environment.get("REDIS_PORT"), "6379")
            self.assertNotIn(
                "REDIS_AUTH",
                service_environment,
                msg=f"{service_name} must not AUTH against the unauthenticated shared Valkey",
            )

        valkey_command = config["services"]["redis"].get("command", [])
        if isinstance(valkey_command, list):
            valkey_command = " ".join(valkey_command)
        else:
            valkey_command = str(valkey_command or "")
        self.assertNotIn("requirepass", valkey_command.lower())

        n8n_environment = config["services"]["n8n"].get("environment", {})
        self.assertEqual(n8n_environment.get("QUEUE_BULL_REDIS_HOST"), "redis")
        self.assertEqual(n8n_environment.get("QUEUE_BULL_REDIS_PORT"), "6379")

    def test_passwordless_valkey_uses_supported_authless_connection_string(self):
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "COMSPEC")
            if key in os.environ
        }
        environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "COMPOSE_PROFILES": "langfuse",
            }
        )

        with tempfile.TemporaryDirectory() as docker_config:
            environment["DOCKER_CONFIG"] = docker_config
            result = subprocess.run(
                compose_command()
                + ["--profile", "langfuse", "config", "--format", "json"],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"Docker Compose config failed with exit code {result.returncode}",
        )
        config = json.loads(result.stdout)

        for service_name in ("langfuse-worker", "langfuse-web"):
            service_environment = config["services"][service_name].get("environment", {})
            connection_string = service_environment.get("REDIS_CONNECTION_STRING")
            self.assertEqual(
                connection_string,
                "redis://redis:6379",
                msg=(
                    f"{service_name} must select Langfuse's authless connection-string "
                    "branch when the shared Valkey has no password"
                ),
            )
            parsed = urlsplit(connection_string or "")
            self.assertEqual(parsed.scheme, "redis")
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertNotIn("REDIS_AUTH", service_environment)

    def test_rehearsal_compose_file_is_valid(self):
        compose = compose_command()
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "COMSPEC")
            if key in os.environ
        }
        environment["COMPOSE_DISABLE_ENV_FILE"] = "1"

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            compose_file = temporary_path / "docker-compose.rehearsal.yml"
            compose_file.write_text(docker_rehearsal_compose(), encoding="utf-8")
            (temporary_path / "probe.js").write_text(
                docker_rehearsal_probe(), encoding="utf-8"
            )
            environment["DOCKER_CONFIG"] = str(temporary_path / "docker-config")
            Path(environment["DOCKER_CONFIG"]).mkdir()
            result = subprocess.run(
                compose
                + ["--file", str(compose_file), "config", "--quiet"],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stdout + result.stderr)[-6000:],
        )

    @unittest.skipUnless(
        os.environ.get("RUN_LANGFUSE_DOCKER_REHEARSAL") == "1",
        "set RUN_LANGFUSE_DOCKER_REHEARSAL=1 to run the disposable Linux Docker rehearsal",
    )
    def test_passwordless_valkey_docker_rehearsal(self):
        compose = compose_command()
        docker = shutil.which("docker")
        if docker is None:
            raise unittest.SkipTest("Docker Engine is required for the Linux rehearsal")
        daemon = subprocess.run(
            [docker, "info"], check=False, capture_output=True, text=True, timeout=15
        )
        if daemon.returncode != 0:
            raise unittest.SkipTest("Docker Engine is not available for the Linux rehearsal")
        project_name = f"langfuse-redis-auth-rehearsal-{os.getpid()}"

        def run_compose(arguments, timeout):
            return subprocess.run(
                compose
                + [
                    "--project-name",
                    project_name,
                    "--file",
                    str(compose_file),
                    *arguments,
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            compose_file = temporary_path / "docker-compose.rehearsal.yml"
            compose_file.write_text(docker_rehearsal_compose(), encoding="utf-8")
            (temporary_path / "probe.js").write_text(
                docker_rehearsal_probe(), encoding="utf-8"
            )
            docker_config = temporary_path / "docker-config"
            docker_config.mkdir()
            environment = {
                key: os.environ[key]
                for key in ("PATH", "SystemRoot", "COMSPEC")
                if key in os.environ
            }
            environment.update(
                {
                    "COMPOSE_DISABLE_ENV_FILE": "1",
                    "DOCKER_CONFIG": str(docker_config),
                }
            )

            try:
                up = run_compose(
                    [
                        "up",
                        "--detach",
                        "--wait",
                        "--wait-timeout",
                        "240",
                    ],
                    timeout=300,
                )
                if up.returncode != 0:
                    self.fail(
                        "Disposable Langfuse stack did not become ready:\n"
                        + (up.stdout + up.stderr)[-6000:]
                    )

                first_probe = run_compose(
                    [
                        "--profile",
                        "probe",
                        "run",
                        "--rm",
                        "--no-deps",
                        "queue-probe",
                    ],
                    timeout=60,
                )
                self.assertEqual(
                    first_probe.returncode,
                    0,
                    msg=(first_probe.stdout + first_probe.stderr)[-6000:],
                )
                self.assertIn("QUEUE_JOB_OK", first_probe.stdout)

                restarted = run_compose(["restart", "redis"], timeout=60)
                self.assertEqual(
                    restarted.returncode,
                    0,
                    msg=(restarted.stdout + restarted.stderr)[-6000:],
                )
                ready_again = run_compose(
                    ["up", "--detach", "--wait", "--wait-timeout", "120"], timeout=180
                )
                self.assertEqual(
                    ready_again.returncode,
                    0,
                    msg=(ready_again.stdout + ready_again.stderr)[-6000:],
                )

                second_probe = run_compose(
                    [
                        "--profile",
                        "probe",
                        "run",
                        "--rm",
                        "--no-deps",
                        "queue-probe",
                    ],
                    timeout=60,
                )
                self.assertEqual(
                    second_probe.returncode,
                    0,
                    msg=(second_probe.stdout + second_probe.stderr)[-6000:],
                )
                self.assertIn("QUEUE_JOB_OK", second_probe.stdout)

                monitor = run_compose(
                    ["logs", "--no-color", "--timestamps", "redis-monitor"], timeout=30
                )
                self.assertEqual(
                    monitor.returncode,
                    0,
                    msg=(monitor.stdout + monitor.stderr)[-6000:],
                )
                self.assertNotRegex(
                    monitor.stdout,
                    r"(?mi)(?:^|[ \"'])AUTH(?:[\" ']|$)",
                    "Valkey MONITOR observed an AUTH command",
                )

                logs = run_compose(
                    [
                        "logs",
                        "--no-color",
                        "--timestamps",
                        "langfuse-worker",
                        "langfuse-web",
                    ],
                    timeout=30,
                )
                self.assertEqual(
                    logs.returncode,
                    0,
                    msg=(logs.stdout + logs.stderr)[-6000:],
                )
                self.assertNotRegex(
                    logs.stdout,
                    r"(?i)(ERR AUTH|without any password configured|NOAUTH|WRONGPASS)",
                )
            finally:
                cleanup = run_compose(
                    ["down", "--volumes", "--remove-orphans"], timeout=120
                )
                if cleanup.returncode != 0 and sys.exc_info()[0] is None:
                    self.fail(
                        "Disposable Langfuse stack cleanup failed:\n"
                        + (cleanup.stdout + cleanup.stderr)[-6000:]
                    )


if __name__ == "__main__":
    unittest.main()

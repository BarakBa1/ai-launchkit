import json
import os
import shutil
import subprocess
import tempfile
import unittest
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

    raise AssertionError("Docker Compose CLI is required to render docker-compose.yml")


class LangfuseRedisAuthTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

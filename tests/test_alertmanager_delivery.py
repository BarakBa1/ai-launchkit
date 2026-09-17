import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPOSITORY_ROOT / "monitoring" / "validate_alertmanager_delivery.py"


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


def compose_config(profile, **extra_environment):
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "COMSPEC")
        if key in os.environ
    }
    environment.update(
        {
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "COMPOSE_PROFILES": profile,
        }
    )
    environment.update(extra_environment)
    result = subprocess.run(
        compose_command()
        + ["--profile", profile, "config", "--format", "json"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Compose config failed with exit code {result.returncode}: {result.stderr}"
        )
    return json.loads(result.stdout)


class AlertmanagerDeliveryTests(unittest.TestCase):
    def test_alertmanager_service_is_pinned_and_hardened(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = compose_config(
                "monitoring",
                ALERTMANAGER_SLACK_WEBHOOK_FILE=str(
                    Path(temp_dir) / "alertmanager-slack-webhook"
                ),
            )

        service = config["services"]["alertmanager"]
        self.assertEqual(service["image"], "prom/alertmanager:v0.28.1")
        self.assertEqual(service["profiles"], ["monitoring"])
        self.assertEqual(service["user"], "65534:65534")
        self.assertTrue(service["read_only"])
        self.assertIn("ALL", service["cap_drop"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertIn("/tmp", service["tmpfs"])
        self.assertNotIn("ports", service)
        self.assertIn("alertmanager_slack_webhook", config["secrets"])
        self.assertIn("healthcheck", service)

        prometheus = config["services"]["prometheus"]
        self.assertEqual(
            prometheus["depends_on"]["alertmanager"]["condition"],
            "service_healthy",
        )
        self.assertIn("healthcheck", prometheus)

        entrypoint = (
            REPOSITORY_ROOT / "alertmanager" / "entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("/run/secrets/alertmanager_slack_webhook", entrypoint)
        self.assertIn("-gt 4096", entrypoint)
        self.assertIn("https://", entrypoint)
        self.assertIn("exit 64", entrypoint)

    def test_prometheus_alerts_to_internal_alertmanager_and_loads_watchdog_rules(self):
        prometheus = yaml.safe_load(
            (REPOSITORY_ROOT / "prometheus" / "prometheus.yml").read_text(
                encoding="utf-8"
            )
        )

        alertmanagers = prometheus["alerting"]["alertmanagers"]
        targets = alertmanagers[0]["static_configs"][0]["targets"]
        self.assertEqual(targets, ["alertmanager:9093"])
        self.assertIn("/etc/prometheus/rules/*.yml", prometheus["rule_files"])
        scrape_jobs = {job["job_name"]: job for job in prometheus["scrape_configs"]}
        self.assertEqual(
            scrape_jobs["n8n-queue-watchdog"]["static_configs"][0]["targets"],
            ["n8n-queue-watchdog:9105"],
        )

    def test_alertmanager_routes_only_watchdog_critical_and_warning_alerts_to_slack(self):
        alertmanager = yaml.safe_load(
            (REPOSITORY_ROOT / "alertmanager" / "alertmanager.yml").read_text(
                encoding="utf-8"
            )
        )
        route = alertmanager["route"]
        self.assertEqual(route["receiver"], "discard")
        watchdog_route = route["routes"][0]
        self.assertEqual(watchdog_route["receiver"], "n8n-queue-watchdog-slack")
        self.assertEqual(
            watchdog_route["matchers"],
            [
                'component=~"n8n-queue(-watchdog)?"',
                'severity=~"critical|warning"',
            ],
        )
        slack = alertmanager["receivers"][1]["slack_configs"][0]
        self.assertEqual(
            slack["api_url_file"], "/run/secrets/alertmanager_slack_webhook"
        )
        self.assertNotIn("api_url", slack)

    def test_every_watchdog_rule_has_a_routeable_component_and_severity(self):
        rules = yaml.safe_load(
            (REPOSITORY_ROOT / "prometheus" / "rules" / "n8n_queue_watchdog.yml").read_text(
                encoding="utf-8"
            )
        )
        for rule in rules["groups"][0]["rules"]:
            with self.subTest(alert=rule["alert"]):
                self.assertIn(
                    rule["labels"].get("component"),
                    ("n8n-queue-watchdog", "n8n-queue"),
                )
                self.assertIn(rule["labels"].get("severity"), ("critical", "warning"))

    def test_monitoring_delivery_validator_fails_without_a_nonempty_receiver_secret(self):
        environment = {
            "COMPOSE_PROFILES": "n8n,monitoring",
            "PATH": os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            [os.environ.get("PYTHON", "python"), str(VALIDATOR)],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ALERTMANAGER_SLACK_WEBHOOK_FILE", result.stderr)

    def test_monitoring_delivery_validator_accepts_a_nonempty_receiver_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_path = Path(temp_dir) / "alertmanager-slack-webhook"
            secret_path.write_text(
                "https://hooks.slack.example/services/test-only\n",
                encoding="utf-8",
            )
            environment = {
                "COMPOSE_PROFILES": "monitoring",
                "ALERTMANAGER_SLACK_WEBHOOK_FILE": str(secret_path),
                "PATH": os.environ.get("PATH", ""),
            }
            result = subprocess.run(
                [os.environ.get("PYTHON", "python"), str(VALIDATOR)],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_monitoring_delivery_validator_rejects_non_https_receiver_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_path = Path(temp_dir) / "alertmanager-slack-webhook"
            secret_path.write_text(
                "http://hooks.slack.example/services/test-only\n",
                encoding="utf-8",
            )
            environment = {
                "COMPOSE_PROFILES": "monitoring",
                "ALERTMANAGER_SLACK_WEBHOOK_FILE": str(secret_path),
                "PATH": os.environ.get("PATH", ""),
            }
            result = subprocess.run(
                [os.environ.get("PYTHON", "python"), str(VALIDATOR)],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ALERTMANAGER_SLACK_WEBHOOK_FILE", result.stderr)

    def test_non_monitoring_compose_config_does_not_require_alertmanager_secret(self):
        config = compose_config("n8n")
        self.assertNotIn("alertmanager", config["services"])
        self.assertNotIn("prometheus", config["services"])

    def test_operator_wiring_mentions_secret_validation_without_exposing_secret(self):
        env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
        wizard = (REPOSITORY_ROOT / "scripts" / "04_wizard.sh").read_text(
            encoding="utf-8"
        )
        startup = (REPOSITORY_ROOT / "scripts" / "05_run_services.sh").read_text(
            encoding="utf-8"
        )
        report = (REPOSITORY_ROOT / "scripts" / "06_final_report.sh").read_text(
            encoding="utf-8"
        )
        readme = (REPOSITORY_ROOT / "monitoring" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("ALERTMANAGER_SLACK_WEBHOOK_FILE=", env_example)
        self.assertIn("Prometheus, Alertmanager, Slack alerts", wizard)
        self.assertIn("validate_alertmanager_delivery.py", startup)
        self.assertIn("alertmanager:9093", report)
        self.assertIn("root-owned", readme)
        self.assertNotIn("no Alertmanager service", readme)


if __name__ == "__main__":
    unittest.main()

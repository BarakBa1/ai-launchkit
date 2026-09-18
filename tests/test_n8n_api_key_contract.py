import shutil
import subprocess
import unittest
import os
import json
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from pathlib import Path

try:
    import start_services
except ModuleNotFoundError as error:
    if error.name != "dotenv":
        raise
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.dotenv_values = lambda *_args, **_kwargs: {}
    sys.modules["dotenv"] = dotenv_stub
    import start_services


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UTILS_PATH = REPOSITORY_ROOT / "scripts" / "utils.sh"


def bash_executable():
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramW6432", "")) / "Git" / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


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


def node_executable():
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node.js is required for n8n-mcp entrypoint tests")
    return node


class N8nApiKeyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if bash_executable() is None:
            raise unittest.SkipTest("bash is required for shell contract tests")

    def run_validator(self, profiles, key):
        command = (
            f'source "{UTILS_PATH}"; '
            f'require_n8n_mcp_api_key "{profiles}" "{key}"'
        )
        environment = os.environ.copy()
        if os.name == "nt":
            git_usr_bin = Path(os.environ.get("ProgramFiles", "")) / "Git" / "usr" / "bin"
            environment["PATH"] = os.pathsep.join(
                [str(git_usr_bin), environment.get("PATH", "")]
            )
        return subprocess.run(
            [bash_executable(), "-c", command],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def run_shape_hint(self, key):
        command = f'source "{UTILS_PATH}"; n8n_public_api_key_shape_hint "{key}"'
        environment = os.environ.copy()
        if os.name == "nt":
            git_usr_bin = Path(os.environ.get("ProgramFiles", "")) / "Git" / "usr" / "bin"
            environment["PATH"] = os.pathsep.join(
                [str(git_usr_bin), environment.get("PATH", "")]
            )
        return subprocess.run(
            [bash_executable(), "-c", command],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def run_live_validator(self, profiles, key, url, status="401", curl_exit=0):
        environment = os.environ.copy()
        if os.name == "nt":
            git_usr_bin = Path(os.environ.get("ProgramFiles", "")) / "Git" / "usr" / "bin"
            environment["PATH"] = os.pathsep.join(
                [str(git_usr_bin), environment.get("PATH", "")]
            )
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker_path = Path(temporary_directory) / "curl.called"
            command = (
                f'source "{UTILS_PATH}"; '
                f'curl() {{ printf "%s" "{status}"; : > "{marker_path}"; return {curl_exit}; }}; '
                f'require_n8n_mcp_api_key_live "{profiles}" "{url}"; '
                f'rc=$?; if [ -f "{marker_path}" ]; then printf "\\n__curl_called=1\\n"; '
                f'else printf "\\n__curl_called=0\\n"; fi; exit "$rc"'
            )
            environment["N8N_API_KEY"] = key
            environment["N8N_API_KEY_LIVE_RETRY_DELAY_SECONDS"] = "0"
            return subprocess.run(
                [bash_executable(), "-c", command],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

    def render_compose(self, profiles, api_key=None, mcp_token="synthetic-mcp-token"):
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "COMSPEC")
            if key in os.environ
        }
        environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "COMPOSE_PROFILES": profiles,
                "N8N_MCP_TOKEN": mcp_token,
            }
        )
        if api_key is not None:
            environment["N8N_API_KEY"] = api_key

        with tempfile.TemporaryDirectory() as docker_config:
            environment["DOCKER_CONFIG"] = docker_config
            command = [*compose_command(), "config", "--format", "json"]
            result = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(result.returncode, 0)
        return json.loads(result.stdout)

    def test_generator_does_not_classify_n8n_api_key_as_generated_secret(self):
        source = (REPOSITORY_ROOT / "scripts" / "03_generate_secrets.sh").read_text(encoding="utf-8")

        self.assertNotRegex(
            source,
            r'\["N8N_API_KEY"\]\s*=\s*"apikey:',
            "N8N_API_KEY must be supplied externally, not generated by the wizard",
        )

    def test_generator_keeps_inbound_mcp_token_generation(self):
        source = (REPOSITORY_ROOT / "scripts" / "03_generate_secrets.sh").read_text(encoding="utf-8")
        self.assertRegex(source, r'\["N8N_MCP_TOKEN"\]\s*=\s*"apikey:32"')

    def test_generator_validates_an_existing_n8n_mcp_profile_before_side_effects(self):
        source = (REPOSITORY_ROOT / "scripts" / "03_generate_secrets.sh").read_text(encoding="utf-8")
        validation = source.index("require_n8n_mcp_api_key")
        caddy_install = source.index("# Install Caddy")
        self.assertLess(validation, caddy_install)

    def test_wizard_validates_selected_n8n_mcp_profile(self):
        source = (REPOSITORY_ROOT / "scripts" / "04_wizard.sh").read_text(encoding="utf-8")
        validation = source.index("require_n8n_mcp_api_key")
        profile_write = source.index("# Update or add COMPOSE_PROFILES")
        self.assertLess(validation, profile_write)

    def test_service_runner_validates_before_launching_services(self):
        source = (REPOSITORY_ROOT / "scripts" / "05_run_services.sh").read_text(encoding="utf-8")
        validation = source.index('require_n8n_mcp_api_key "${COMPOSE_PROFILES:-}"')
        launch = source.index("./start_services.py")
        self.assertLess(validation, launch)
        self.assertNotIn(
            'require_n8n_mcp_api_key_live "${COMPOSE_PROFILES:-}"',
            source,
        )

    def test_n8n_mcp_rejection_does_not_echo_supplied_secret(self):
        generic_secret = "a" * 32
        result = self.run_validator("n8n-mcp", generic_secret)
        self.assertNotIn(generic_secret, result.stdout + result.stderr)

    def test_n8n_mcp_requires_an_external_public_api_jwt(self):
        result = self.run_validator("n8n,n8n-mcp", "")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("N8N_API_KEY", result.stdout + result.stderr)

    def test_live_check_rejects_missing_api_key_without_network_probe(self):
        result = self.run_live_validator("n8n-mcp", "", "https://n8n.example.test")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("__curl_called=0", result.stdout + result.stderr)

    def test_live_check_rejects_malformed_api_key_without_network_probe(self):
        result = self.run_live_validator(
            "n8n-mcp", "not-a-jwt", "https://n8n.example.test"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("__curl_called=0", result.stdout + result.stderr)

    def test_live_check_rejects_expired_jwt_hint_without_network_probe(self):
        expired_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJwdWJsaWMtYXBpIiwiZXhwIjoxfQ."
            "forged-signature"
        )
        result = self.run_live_validator(
            "n8n-mcp", expired_jwt, "https://n8n.example.test"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("__curl_called=0", result.stdout + result.stderr)

    def test_live_check_rejects_forged_api_key_from_authoritative_401(self):
        forged_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJwdWJsaWMtYXBpIn0."
            "forged-signature"
        )
        result = self.run_live_validator(
            "n8n-mcp", forged_jwt, "https://n8n.example.test"
        )

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("__curl_called=1", output)
        self.assertIn("HTTP 401", output)
        self.assertNotIn(forged_jwt, output)

    def test_live_check_requires_an_https_n8n_url_without_network_probe(self):
        shaped_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJwdWJsaWMtYXBpIn0."
            "shaped-only-signature"
        )
        result = self.run_live_validator(
            "n8n-mcp", shaped_jwt, "http://n8n.example.test", status="200"
        )

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("N8N_URL", output)
        self.assertIn("__curl_called=0", output)

    def test_live_check_rejects_unreachable_public_url_without_echo(self):
        shaped_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJwdWJsaWMtYXBpIn0."
            "shaped-only-signature"
        )
        result = self.run_live_validator(
            "n8n-mcp", shaped_jwt, "https://n8n.example.test", curl_exit=7
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(shaped_jwt, result.stdout + result.stderr)
        self.assertIn("network error", result.stdout + result.stderr)

    def test_live_check_skips_api_probe_for_base_profiles(self):
        result = self.run_live_validator("n8n", "not-a-jwt", "", status="401")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("__curl_called=0", result.stdout + result.stderr)

    def test_service_runner_uses_bounded_live_api_validation(self):
        source = (REPOSITORY_ROOT / "scripts" / "utils.sh").read_text(encoding="utf-8")
        self.assertIn("--connect-timeout 5", source)
        self.assertIn("--max-time 10", source)
        self.assertIn("/api/v1/workflows?limit=1", source)
        self.assertIn("--header @-", source)
        self.assertNotIn('--header "X-N8N-API-KEY: $key"', source)

    def test_n8n_mcp_rejects_a_generic_hex_secret(self):
        result = self.run_validator("n8n-mcp", "a" * 32)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("public API JWT", result.stdout + result.stderr)

    def test_non_n8n_mcp_profiles_allow_missing_api_key(self):
        result = self.run_validator("n8n,langfuse", "")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_n8n_mcp_profiles_ignore_an_invalid_api_key(self):
        result = self.run_validator("n8n,langfuse", "not-a-jwt")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_shape_hint_only_recognizes_public_api_audience_shape(self):
        synthetic_public_api_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJwdWJsaWMtYXBpIn0."
            "synthetic-signature"
        )

        result = self.run_shape_hint(synthetic_public_api_jwt)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_n8n_mcp_rejects_a_jwt_for_a_different_audience(self):
        wrong_audience_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJvdGhlciJ9."
            "synthetic-signature"
        )
        result = self.run_validator("n8n-mcp", wrong_audience_jwt)

        self.assertNotEqual(result.returncode, 0)

    def test_compose_maps_distinct_mcp_and_public_api_credentials(self):
        synthetic_public_api_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJhdWQiOiJwdWJsaWMtYXBpIn0."
            "synthetic-signature"
        )
        config = self.render_compose("n8n-mcp", api_key=synthetic_public_api_jwt)
        service_environment = config["services"]["n8n-mcp"].get("environment", {})
        self.assertEqual(service_environment.get("AUTH_TOKEN"), "synthetic-mcp-token")
        self.assertEqual(service_environment.get("N8N_API_KEY"), synthetic_public_api_jwt)

    def test_compose_without_n8n_mcp_allows_missing_public_api_key(self):
        config = self.render_compose("n8n", api_key=None)
        self.assertNotIn("n8n-mcp", config["services"])

    def test_compose_mcp_healthcheck_only_reports_live_mcp_process(self):
        config = self.render_compose("n8n-mcp", api_key="")
        healthcheck = config["services"]["n8n-mcp"]["healthcheck"]["test"]
        healthcheck_command = " ".join(str(part) for part in healthcheck)
        self.assertIn("curl", healthcheck_command)
        self.assertIn("/health", healthcheck_command)
        self.assertNotIn("N8N_API_KEY", healthcheck_command)

        result = self.run_compose_healthcheck(config, "", "200")
        self.assertEqual(result.returncode, 0)

    def test_compose_mcp_healthcheck_does_not_revalidate_secret_in_process_args(self):
        config = self.render_compose("n8n-mcp", api_key="forged-api-key")
        healthcheck = config["services"]["n8n-mcp"]["healthcheck"]["test"]
        healthcheck_command = " ".join(str(part) for part in healthcheck)
        self.assertNotIn("X-N8N-API-KEY", healthcheck_command)
        self.assertNotIn("N8N_API_KEY", healthcheck_command)

        result = self.run_compose_healthcheck(config, "forged-api-key", "401")
        self.assertEqual(result.returncode, 0)
        self.assertIn("__curl_called", result.stdout + result.stderr)

    def test_direct_mcp_profile_selects_core_n8n_services(self):
        config = self.render_compose("n8n-mcp", api_key="forged-api-key")

        self.assertIn("n8n-import", config["services"])
        self.assertIn("n8n", config["services"])

    def test_n8n_is_healthy_before_mcp_can_start(self):
        config = self.render_compose(
            "n8n,n8n-mcp", api_key="forged-api-key"
        )
        n8n_healthcheck = config["services"]["n8n"].get("healthcheck")
        mcp_depends_on = config["services"]["n8n-mcp"].get("depends_on", {})

        self.assertIsNotNone(n8n_healthcheck)
        self.assertEqual(
            mcp_depends_on.get("n8n", {}).get("condition"),
            "service_healthy",
        )

    def test_mcp_override_retains_image_command_and_original_entrypoint(self):
        config = self.render_compose("n8n-mcp", api_key="forged-api-key")
        service = config["services"]["n8n-mcp"]

        self.assertEqual(
            service.get("entrypoint"),
            ["node", "/usr/local/bin/n8n_mcp_entrypoint.js"],
        )
        self.assertIsNone(service.get("command"))

    def test_mcp_entrypoint_gates_original_command_on_authoritative_api(self):
        entrypoint = REPOSITORY_ROOT / "scripts" / "n8n_mcp_entrypoint.js"

        self.assertTrue(entrypoint.is_file())
        source = entrypoint.read_text(encoding="utf-8")
        self.assertIn("N8N_API_KEY", source)
        self.assertIn("N8N_API_URL", source)
        self.assertIn("X-N8N-API-KEY", source)
        self.assertIn("spawn(originalEntrypoint, process.argv.slice(2)", source)
        self.assertIn("result.statusCode >= 200 && result.statusCode < 300", source)
        self.assertIn("result.statusCode === 401", source)

    def test_mcp_entrypoint_does_not_put_secrets_in_process_arguments(self):
        compose_source = (REPOSITORY_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        entrypoint = (
            REPOSITORY_ROOT / "scripts" / "n8n_mcp_entrypoint.js"
        ).read_text(encoding="utf-8")

        self.assertIn("n8n_mcp_entrypoint.js", compose_source)
        self.assertNotRegex(
            compose_source,
            r"(?:command|entrypoint):[^\n]*N8N_API_KEY",
        )
        self.assertNotIn("process.env.N8N_API_KEY", entrypoint.split("spawn(", 1)[-1])
        self.assertNotIn("N8N_API_KEY", entrypoint.split("spawn(", 1)[-1])

    def test_mcp_entrypoint_fails_closed_before_original_command_for_missing_key(self):
        source = (
            REPOSITORY_ROOT / "scripts" / "n8n_mcp_entrypoint.js"
        ).read_text(encoding="utf-8")
        missing_key_guard = source.index("N8N_API_KEY")
        original_command = source.index("spawn(originalEntrypoint, process.argv.slice(2)")

        self.assertLess(missing_key_guard, original_command)
        self.assertIn("process.exitCode = 1", source)

    def test_mcp_entrypoint_retries_only_readiness_and_rejects_forged_key(self):
        source = (
            REPOSITORY_ROOT / "scripts" / "n8n_mcp_entrypoint.js"
        ).read_text(encoding="utf-8")

        self.assertIn("READINESS_ATTEMPTS", source)
        self.assertIn("setTimeout", source)
        self.assertIn("networkError", source)
        self.assertIn("authoritative", source.lower())

    def run_mcp_entrypoint(self, api_key, response_statuses, marker_path):
        observations = {"requests": 0, "api_keys": []}

        class ApiHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                observations["requests"] += 1
                observations["api_keys"].append(self.headers.get("X-N8N-API-KEY"))
                self.send_response(response_statuses[min(
                    observations["requests"] - 1, len(response_statuses) - 1
                )])
                self.end_headers()

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), ApiHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            command = [
                node_executable(),
                str(REPOSITORY_ROOT / "scripts" / "n8n_mcp_entrypoint.js"),
                "-e",
                "require('fs').writeFileSync(process.env.MCP_TEST_MARKER, 'started')",
            ]
            environment = os.environ.copy()
            environment.update(
                {
                    "N8N_API_URL": f"http://127.0.0.1:{server.server_port}",
                    "MCP_TEST_MARKER": str(marker_path),
                    "N8N_MCP_READINESS_DELAY_MS": "1",
                    "N8N_MCP_ORIGINAL_ENTRYPOINT": node_executable(),
                }
            )
            if api_key is None:
                environment.pop("N8N_API_KEY", None)
            else:
                environment["N8N_API_KEY"] = api_key
            return subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            ), observations
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_mcp_entrypoint_missing_key_never_starts_original_command(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker_path = Path(temporary_directory) / "started"
            result, observations = self.run_mcp_entrypoint(
                None, [200], marker_path
            )
            self.assertFalse(marker_path.exists())

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(observations["requests"], 0)
        self.assertNotIn("started", result.stdout + result.stderr)

    def test_mcp_entrypoint_forged_key_never_starts_original_command(self):
        forged_key = "forged-public-api-jwt"
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker_path = Path(temporary_directory) / "started"
            result, observations = self.run_mcp_entrypoint(
                forged_key, [401], marker_path
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker_path.exists())
        self.assertEqual(observations["requests"], 1)
        self.assertEqual(observations["api_keys"], [forged_key])
        self.assertNotIn(forged_key, result.stdout + result.stderr)

    def test_mcp_entrypoint_starts_original_command_only_after_authoritative_acceptance(self):
        accepted_key = "n8n-issued-public-api-jwt"
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker_path = Path(temporary_directory) / "started"
            result, observations = self.run_mcp_entrypoint(
                accepted_key, [200], marker_path
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(marker_path.exists())
            self.assertEqual(observations["requests"], 1)
            self.assertEqual(observations["api_keys"], [accepted_key])
            self.assertNotIn(accepted_key, result.stdout + result.stderr)

    def test_mcp_entrypoint_waits_for_n8n_readiness_before_starting_original_command(self):
        accepted_key = "n8n-issued-public-api-jwt"
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker_path = Path(temporary_directory) / "started"
            result, observations = self.run_mcp_entrypoint(
                accepted_key, [503, 200], marker_path
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(marker_path.exists())
            self.assertGreaterEqual(observations["requests"], 2)

    def test_installer_public_preflight_rejects_failed_mcp_validation(self):
        self.assertTrue(
            hasattr(start_services, "run_n8n_mcp_public_preflight"),
            "installer lacks the authoritative n8n-mcp public preflight",
        )
        env_values = {
            "COMPOSE_PROFILES": "n8n,n8n-mcp",
            "N8N_API_KEY": "synthetic-installer-key",
            "N8N_URL": "https://n8n.example.test",
        }
        failed = subprocess.CompletedProcess([], 1)

        with patch.object(start_services.subprocess, "run", return_value=failed) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                start_services.run_n8n_mcp_public_preflight(env_values)

        command = run.call_args.args[0]
        self.assertNotIn(env_values["N8N_API_KEY"], command)
        self.assertIn("$N8N_URL", command[-1])
        self.assertEqual(run.call_args.kwargs["env"]["N8N_API_KEY"], env_values["N8N_API_KEY"])

    def test_installer_public_preflight_skips_base_profiles(self):
        self.assertTrue(
            hasattr(start_services, "run_n8n_mcp_public_preflight"),
            "installer lacks the authoritative n8n-mcp public preflight",
        )
        with patch.object(start_services.subprocess, "run") as run:
            start_services.run_n8n_mcp_public_preflight(
                {"COMPOSE_PROFILES": "n8n", "N8N_API_KEY": "", "N8N_URL": ""}
            )

        run.assert_not_called()

    def test_installer_orders_core_readiness_public_preflight_then_full_launch(self):
        self.assertTrue(
            hasattr(start_services, "run_n8n_mcp_public_preflight"),
            "installer lacks the authoritative n8n-mcp public preflight",
        )
        env_values = {
            "COMPOSE_PROFILES": "n8n,n8n-mcp",
            "N8N_API_KEY": "synthetic-installer-key",
            "N8N_URL": "https://n8n.example.test",
        }
        with patch.object(start_services, "dotenv_values", return_value=env_values), \
             patch.object(start_services, "run_command") as run_command, \
             patch.object(start_services, "run_n8n_mcp_public_preflight") as preflight:
            start_services.start_local_ai()

        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertEqual(commands[0][0:6], ["docker", "compose", "-p", "localai", "-f", "docker-compose.yml"])
        self.assertIn("build", commands[0])
        self.assertIn("--wait", commands[1])
        self.assertIn("caddy", commands[1])
        self.assertIn("n8n-import", commands[1])
        self.assertIn("n8n", commands[1])
        self.assertEqual(commands[-1][-2:], ["up", "-d"])
        self.assertEqual(preflight.call_count, 1)

    def test_installer_does_not_launch_full_stack_when_public_preflight_fails(self):
        self.assertTrue(
            hasattr(start_services, "run_n8n_mcp_public_preflight"),
            "installer lacks the authoritative n8n-mcp public preflight",
        )
        env_values = {
            "COMPOSE_PROFILES": "n8n,n8n-mcp",
            "N8N_API_KEY": "synthetic-installer-key",
            "N8N_URL": "https://n8n.example.test",
        }
        failure = subprocess.CalledProcessError(1, ["bash"])
        with patch.object(start_services, "dotenv_values", return_value=env_values), \
             patch.object(start_services, "run_command") as run_command, \
             patch.object(
                 start_services,
                 "run_n8n_mcp_public_preflight",
                 side_effect=failure,
             ):
            with self.assertRaises(subprocess.CalledProcessError):
                start_services.start_local_ai()

        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("--wait", commands[-1])
        self.assertIn("n8n", commands[-1])

    def test_installer_base_profile_keeps_single_full_launch(self):
        self.assertTrue(
            hasattr(start_services, "run_n8n_mcp_public_preflight"),
            "installer lacks the authoritative n8n-mcp public preflight",
        )
        env_values = {"COMPOSE_PROFILES": "n8n"}
        with patch.object(start_services, "dotenv_values", return_value=env_values), \
             patch.object(start_services, "run_command") as run_command, \
             patch.object(start_services, "run_n8n_mcp_public_preflight") as preflight:
            start_services.start_local_ai()

        commands = [call.args[0] for call in run_command.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("build", commands[0])
        self.assertEqual(commands[1][-2:], ["up", "-d"])
        preflight.assert_not_called()

    def run_compose_healthcheck(self, config, api_key, status):
        healthcheck = config["services"]["n8n-mcp"]["healthcheck"]["test"]
        healthcheck_command = str(healthcheck[-1]).replace("$$", "$")
        command = (
            f'curl() {{ printf "%s" "{status}"; printf "__curl_called\\n" >&2; }}; '
            f'{healthcheck_command}'
        )
        environment = os.environ.copy()
        if os.name == "nt":
            git_usr_bin = Path(os.environ.get("ProgramFiles", "")) / "Git" / "usr" / "bin"
            environment["PATH"] = os.pathsep.join(
                [str(git_usr_bin), environment.get("PATH", "")]
            )
        environment.update({"N8N_API_URL": "http://n8n:5678"})
        if api_key is not None:
            environment["N8N_API_KEY"] = api_key
        return subprocess.run(
            [bash_executable(), "-c", command],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )


if __name__ == "__main__":
    unittest.main()

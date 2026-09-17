import shutil
import subprocess
import unittest
import os
import json
import tempfile
from pathlib import Path


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
                f'require_n8n_mcp_api_key_live "{profiles}" "{key}" "{url}"; '
                f'rc=$?; if [ -f "{marker_path}" ]; then printf "\\n__curl_called=1\\n"; '
                f'else printf "\\n__curl_called=0\\n"; fi; exit "$rc"'
            )
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
        validation = source.index("require_n8n_mcp_api_key_live")
        launch = source.index("./start_services.py")
        self.assertLess(validation, launch)

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

    def test_live_check_skips_api_probe_for_base_profiles(self):
        result = self.run_live_validator("n8n", "not-a-jwt", "", status="401")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("__curl_called=0", result.stdout + result.stderr)

    def test_service_runner_uses_bounded_live_api_validation(self):
        source = (REPOSITORY_ROOT / "scripts" / "utils.sh").read_text(encoding="utf-8")
        self.assertIn("--connect-timeout 5", source)
        self.assertIn("--max-time 10", source)
        self.assertIn("/api/v1/workflows?limit=1", source)

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

    def test_compose_mcp_healthcheck_fails_closed_for_missing_api_key(self):
        config = self.render_compose("n8n-mcp", api_key="")
        healthcheck = config["services"]["n8n-mcp"]["healthcheck"]["test"]
        healthcheck_command = " ".join(str(part) for part in healthcheck)
        self.assertIn('test -n "$$N8N_API_KEY"', healthcheck_command)
        self.assertIn("X-N8N-API-KEY", healthcheck_command)
        self.assertIn("curl", healthcheck_command)

        result = self.run_compose_healthcheck(config, "", "200")
        self.assertNotEqual(result.returncode, 0)

    def test_compose_mcp_healthcheck_probes_and_rejects_authoritative_401(self):
        config = self.render_compose("n8n-mcp", api_key="forged-api-key")
        healthcheck = config["services"]["n8n-mcp"]["healthcheck"]["test"]
        healthcheck_command = " ".join(str(part) for part in healthcheck)
        self.assertIn('case "$$status" in 2??)', healthcheck_command)
        self.assertIn("exit 1", healthcheck_command)

        result = self.run_compose_healthcheck(config, "forged-api-key", "401")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("__curl_called", result.stdout + result.stderr)

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

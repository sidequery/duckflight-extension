from __future__ import annotations

import importlib.util
import io
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "duckflight_auth.py"
SPEC = importlib.util.spec_from_file_location("duckflight_auth", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
duckflight_auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(duckflight_auth)


class DuckflightAuthTests(unittest.TestCase):
    def test_known_scram_sha256_vector(self) -> None:
        password_hash = duckflight_auth.derive_password_hash(
            "testpass", bytes(range(1, 17)), 4_096
        )
        self.assertEqual(
            password_hash,
            "7903ae83ca06b339de2328863b4ec0cc644b2c8aa4b652f5b9c07518583c3a1d",
        )

    def test_round_trip_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.toml"
            users = {
                "analyst@example.com": {
                    "password_hash": duckflight_auth.derive_password_hash(
                        "correct horse", bytes(range(16)), 10_000
                    ),
                    "salt": list(range(16)),
                    "iterations": 10_000,
                }
            }
            duckflight_auth.write_users(path, users)

            self.assertEqual(duckflight_auth.read_users(path), users)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_mixed_iteration_counts_are_rejected(self) -> None:
        user = {
            "password_hash": "00" * 32,
            "salt": list(range(16)),
            "iterations": 4_096,
        }
        other = dict(user, iterations=10_000)
        with self.assertRaisesRegex(
            duckflight_auth.AuthFileError, "mixed SCRAM iteration counts"
        ):
            duckflight_auth.validate_users({"users": {"one": user, "two": other}})

    def test_user_command_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.toml"
            output = io.StringIO()
            with (
                mock.patch.object(
                    duckflight_auth.getpass,
                    "getpass",
                    side_effect=["secret-password", "secret-password"],
                ),
                redirect_stdout(output),
            ):
                result = duckflight_auth.main(
                    ["user", "add", "alice", "--file", str(path)]
                )
            self.assertEqual(result, 0)
            self.assertIn("alice", duckflight_auth.read_users(path))

            output = io.StringIO()
            with (
                mock.patch.object(
                    duckflight_auth.getpass,
                    "getpass",
                    return_value="secret-password",
                ),
                redirect_stdout(output),
            ):
                result = duckflight_auth.main(
                    ["user", "test", "alice", "--file", str(path)]
                )
            self.assertEqual(result, 0)
            self.assertIn("authentication succeeded", output.getvalue())

            error = io.StringIO()
            with (
                mock.patch.object(
                    duckflight_auth.getpass, "getpass", return_value="wrong"
                ),
                redirect_stderr(error),
            ):
                result = duckflight_auth.main(
                    ["user", "test", "alice", "--file", str(path)]
                )
            self.assertEqual(result, 1)
            self.assertIn("authentication failed", error.getvalue())

            with redirect_stdout(io.StringIO()):
                result = duckflight_auth.main(
                    ["user", "remove", "alice", "--file", str(path)]
                )
            self.assertEqual(result, 0)
            self.assertEqual(duckflight_auth.read_users(path), {})

    def test_token_lifecycle_stores_only_digest_and_preserves_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duckflight.toml"
            users = {
                "alice": {
                    "password_hash": "00" * 32,
                    "salt": list(range(16)),
                    "iterations": 10_000,
                }
            }
            duckflight_auth.write_users(path, users)
            output = io.StringIO()
            with (
                mock.patch.object(
                    duckflight_auth.secrets,
                    "token_urlsafe",
                    return_value="airport-secret-token",
                ),
                redirect_stdout(output),
            ):
                result = duckflight_auth.main(
                    ["token", "add", "airport", "--file", str(path)]
                )
            self.assertEqual(result, 0)
            self.assertIn("token=airport-secret-token", output.getvalue())
            config = duckflight_auth.read_config(path)
            self.assertEqual(config["users"], users)
            self.assertEqual(
                config["tokens"]["airport"]["sha256"],
                duckflight_auth.hashlib.sha256(b"airport-secret-token").hexdigest(),
            )
            self.assertEqual(
                config["tokens"]["airport"]["scopes"],
                ["query:execute", "transaction:manage"],
            )
            self.assertNotIn("airport-secret-token", path.read_text())

            with (
                mock.patch.object(
                    duckflight_auth.getpass,
                    "getpass",
                    return_value="airport-secret-token",
                ),
                redirect_stdout(io.StringIO()),
            ):
                result = duckflight_auth.main(
                    ["token", "test", "airport", "--file", str(path)]
                )
            self.assertEqual(result, 0)

            with redirect_stdout(io.StringIO()):
                result = duckflight_auth.main(
                    ["token", "remove", "airport", "--file", str(path)]
                )
            self.assertEqual(result, 0)
            self.assertEqual(duckflight_auth.read_config(path)["tokens"], {})

    def test_config_rejects_client_ca_without_identity_mapping(self) -> None:
        with self.assertRaisesRegex(
            duckflight_auth.AuthFileError,
            "client_ca and tls.identities must be configured together",
        ):
            duckflight_auth.validate_config(
                {
                    "tls": {
                        "cert": "server.crt",
                        "key": "server.key",
                        "client_ca": "ca.crt",
                    }
                }
            )

    def test_mtls_fingerprints_are_normalized(self) -> None:
        fingerprint = "AB" * 32
        config = duckflight_auth.validate_config(
            {
                "tls": {
                    "cert": "server.crt",
                    "key": "server.key",
                    "client_ca": "ca.crt",
                    "identities": {f"sha256:{fingerprint}": {"subject": "reporter"}},
                }
            }
        )
        self.assertIn(f"sha256:{fingerprint.lower()}", config["tls"]["identities"])


if __name__ == "__main__":
    unittest.main()

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

# Public P-256 key generated with OpenSSL; no private material is retained.
PUBLIC_JWK = {
    "kty": "EC",
    "kid": "test",
    "crv": "P-256",
    "alg": "ES256",
    "x": "NWH-h42o7YC9dM0DGTVsQki1T-k5-GFG76X_KXerILI",
    "y": "V3fOoVGYKAO_zaJSPQUBV-nQfnUBC6EOcFUBYQ-Jx8A",
}


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

    def test_oidc_and_policy_survive_user_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duckflight.toml"
            oidc = [
                {
                    "name": "company",
                    "issuer": "https://issuer.example",
                    "audience": "flight",
                    "required_claims": {"email_verified": True},
                    "algorithms": ["ES256"],
                    "jwks": {"keys": [PUBLIC_JWK]},
                }
            ]
            policy = {
                "rules": [
                    {
                        "provider": "oidc:company",
                        "group": "readers",
                        "scopes": ["query:execute"],
                    }
                ]
            }
            duckflight_auth.write_config(path, {"oidc": oidc, "authorization": policy})
            duckflight_auth.write_users(
                path,
                {
                    "alice": {
                        "password_hash": "00" * 32,
                        "salt": list(range(16)),
                        "iterations": 4096,
                    }
                },
            )
            config = duckflight_auth.read_config(path)
            self.assertEqual(config["oidc"], oidc)
            self.assertEqual(config["authorization"], policy)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_oidc_configuration_fails_closed(self) -> None:
        provider = {
            "name": "company",
            "issuer": "https://issuer.example",
            "audience": "flight",
        }
        invalid = [
            {"oidc": [dict(provider, algorithms=["none"])]},
            {"oidc": [dict(provider, jwks_url="http://issuer.example/keys")]},
            {"oidc": [dict(provider, audience="")]},
            {"oidc": [dict(provider, typo=True)]},
            {"oidc": [provider, provider]},
            {
                "oidc": [provider],
                "authorization": {
                    "rules": [{"provider": "oidc:missing", "scopes": ["query:execute"]}]
                },
            },
            {
                "oidc": [provider],
                "authorization": {
                    "rules": [{"provider": "oidc:company", "scopes": ["query:typo"]}]
                },
            },
        ]
        for config in invalid:
            with (
                self.subTest(config=config),
                self.assertRaises(duckflight_auth.AuthFileError),
            ):
                duckflight_auth.validate_config(config)

    def test_static_jwks_rejects_malformed_or_unusable_keys(self) -> None:
        provider = {
            "name": "company",
            "issuer": "https://issuer.example",
            "audience": "flight",
            "algorithms": ["ES256"],
        }
        invalid = [
            [1],
            [{}],
            [PUBLIC_JWK, PUBLIC_JWK],
            [dict(PUBLIC_JWK, kty="oct")],
            [dict(PUBLIC_JWK, crv="P-384")],
            [dict(PUBLIC_JWK, kid="")],
            [dict(PUBLIC_JWK, kid=1)],
            [dict(PUBLIC_JWK, use="enc")],
            [dict(PUBLIC_JWK, key_ops=["sign"])],
            [dict(PUBLIC_JWK, key_ops="verify")],
            [dict(PUBLIC_JWK, key_ops=[1])],
            [dict(PUBLIC_JWK, alg="RS256")],
            [dict(PUBLIC_JWK, x="a!")],
            [{key: value for key, value in PUBLIC_JWK.items() if key != "y"}],
        ]
        for keys in invalid:
            with (
                self.subTest(keys=keys),
                self.assertRaises(duckflight_auth.AuthFileError),
            ):
                duckflight_auth.validate_config(
                    {"oidc": [dict(provider, jwks={"keys": keys})]}
                )
        with self.assertRaisesRegex(duckflight_auth.AuthFileError, "no usable"):
            duckflight_auth.validate_config(
                {
                    "oidc": [
                        dict(
                            provider, algorithms=["RS256"], jwks={"keys": [PUBLIC_JWK]}
                        )
                    ]
                }
            )

    def test_static_jwks_preserves_unused_provider_keys(self) -> None:
        keys = [
            PUBLIC_JWK,
            dict(PUBLIC_JWK, use="enc"),
            dict(PUBLIC_JWK, key_ops=["sign"]),
        ]
        provider = {
            "name": "company",
            "issuer": "https://issuer.example",
            "audience": "flight",
            "algorithms": ["ES256"],
            "jwks": {"keys": keys},
        }
        config = duckflight_auth.validate_config({"oidc": [provider]})
        self.assertEqual(config["oidc"][0]["jwks"]["keys"], keys)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duckflight.toml"
            duckflight_auth.write_config(path, config)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    duckflight_auth.main(["check", "--file", str(path)]), 0
                )
            self.assertIn("structurally valid", output.getvalue())
            self.assertIn("verified by the runtime", output.getvalue())

    def test_runtime_options_survive_user_edits(self) -> None:
        flight = {
            "max_sessions": 20,
            "session_timeout_ms": 60000,
            "transaction_timeout_ms": 5000,
            "query_timeout_ms": 100,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duckflight.toml"
            duckflight_auth.write_config(
                path, {"shutdown_grace_secs": 0, "flight": flight}
            )
            duckflight_auth.write_users(
                path,
                {
                    "alice": {
                        "password_hash": "00" * 32,
                        "salt": list(range(16)),
                        "iterations": 4096,
                    }
                },
            )
            config = duckflight_auth.read_config(path)
            self.assertEqual(config["flight"], flight)
            self.assertEqual(config["shutdown_grace_secs"], 0)

    def test_runtime_options_reject_wrong_units_and_invalid_values(self) -> None:
        invalid = [
            {"flight": {"query_timeout": "1s"}},
            {"flight": []},
            {"flight": {"max_sessions": 0}},
        ]
        for value in (-1, True, 1.5, "30", 1 << 63):
            invalid.extend(
                [
                    {"shutdown_grace_secs": value},
                    {"flight": {"query_timeout_ms": value}},
                ]
            )
        for config in invalid:
            with (
                self.subTest(config=config),
                self.assertRaises(duckflight_auth.AuthFileError),
            ):
                duckflight_auth.validate_config(config)


if __name__ == "__main__":
    unittest.main()

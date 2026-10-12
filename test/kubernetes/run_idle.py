#!/usr/bin/env -S uv run --script
"""Run the opt-in chart's real PostgreSQL/Flight idle cycle on disposable k3s."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import tempfile
from pathlib import Path

from run import K3S_IMAGE, ROOT, Suite


class IdleSuite(Suite):
    def setup(self) -> None:
        self.command(
            self.arguments.k3d,
            "cluster",
            "create",
            self.cluster,
            "--image",
            K3S_IMAGE,
            "--agents",
            "1",
            "--kubeconfig-update-default=false",
            "--kubeconfig-switch-context=false",
            "--k3s-arg",
            "--disable=traefik@server:0",
            "--k3s-arg",
            "--disable=servicelb@server:0",
            "--wait",
            "--timeout",
            "180s",
        )
        config = self.command(self.arguments.k3d, "kubeconfig", "get", self.cluster)
        (self.directory / "kubeconfig").write_text(config)
        (self.directory / "kubeconfig").chmod(0o600)
        self.command("kubectl", "create", "namespace", self.namespace)
        self.namespace_created = True
        self.command(
            self.arguments.k3d,
            "image",
            "import",
            self.arguments.image,
            self.client_image,
            "--cluster",
            self.cluster,
        )
        self.secret(
            "test-credentials",
            {key: value.encode() for key, value in self.credentials.items()},
        )
        certificate = self.directory / "server.crt"
        key = self.directory / "server.key"
        self.command(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=lake-duckflight",
            "-addext",
            f"subjectAltName=DNS:lake-duckflight,DNS:lake-duckflight.{self.namespace}.svc",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
        )
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", self.credentials["CLIENT_PASSWORD"].encode(), salt, 10000
        ).hex()
        auth = (
            f'[users.e2e]\npassword_hash="{digest}"\nsalt={list(salt)}\niterations=10000\n'
            '[tls]\ncert="server.crt"\nkey="server.key"\n'
        )
        self.secret(
            "worker-auth",
            {
                "duckflight.toml": auth.encode(),
                "server.crt": certificate.read_bytes(),
                "server.key": key.read_bytes(),
            },
        )
        self.secret("client-tls", {"server.crt": certificate.read_bytes()})
        self.secret("worker-bootstrap", {"init.sql": b"select 1;\n"})
        self.secret(
            "idle-control",
            {
                "token": secrets.token_hex(32).encode(),
                "ca.crt": certificate.read_bytes(),
            },
        )
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "idle-client"},
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "idle-client"},
                "rules": [
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments"],
                        "resourceNames": ["lake-duckflight"],
                        "verbs": ["get"],
                    }
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "idle-client"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": "idle-client",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "idle-client",
                        "namespace": self.namespace,
                    }
                ],
            },
        )
        repository, tag = self.arguments.image.rsplit(":", 1)
        values = {
            "image": {"repository": repository, "tag": tag, "pullPolicy": "Never"},
            "auth": {"existingSecret": "worker-auth"},
            "bootstrap": {"existingSecret": "worker-bootstrap"},
            "idleScale": {
                "enabled": True,
                "existingSecret": "idle-control",
                "idleTimeoutSeconds": 10,
            },
            "duckdb": {"memoryLimit": "512MB", "threads": 1},
            "resources": {
                "requests": {
                    "cpu": "100m",
                    "memory": "512Mi",
                    "ephemeral-storage": "256Mi",
                },
                "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            },
        }
        values_file = self.directory / "values.json"
        values_file.write_text(json.dumps(values))
        self.command(
            "helm",
            "upgrade",
            "--install",
            "lake",
            str(ROOT / "charts/duckflight"),
            "--namespace",
            self.namespace,
            "--values",
            str(values_file),
            "--wait",
            "--timeout",
            "5m",
        )

    def run(self) -> None:
        self.setup()
        self.job("idle", service_account="idle-client")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="Locally built idle-capable server image"
    )
    parser.add_argument("--k3d", default="k3d")
    parser.add_argument("--client-image", default="duckflight-idle-e2e-client:local")
    parser.add_argument(
        "--artifacts", type=Path, default=Path("/tmp/duckflight-idle-e2e-artifacts")
    )
    arguments = parser.parse_args()
    arguments.catalog = "idle"
    arguments.cluster = None
    with tempfile.TemporaryDirectory(prefix="duckflight-idle-k3s-") as directory:
        suite = IdleSuite(arguments, Path(directory))
        try:
            suite.run()
        except Exception:
            suite.diagnose()
            raise
        finally:
            suite.command(arguments.k3d, "cluster", "delete", suite.cluster)


if __name__ == "__main__":
    main()

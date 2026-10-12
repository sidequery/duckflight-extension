#!/usr/bin/env -S uv run --script
"""Install the real Helm chart on isolated k3s and query external lake catalogs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT_IMAGE = "duckflight-e2e-client:local"
K3S_IMAGE = "rancher/k3s:v1.33.6-k3s1@sha256:d7119ab9e0054692a86a8eda454f2d3370dafc1c3a96b3ace5c5599f0c1e9f15"


class Suite:
    def __init__(self, arguments, directory: Path):
        self.arguments = arguments
        self.client_image = getattr(arguments, "client_image", CLIENT_IMAGE)
        self.directory = directory
        self.cluster = arguments.cluster or f"duckflight-e2e-{secrets.token_hex(4)}"
        self.namespace = f"lake-e2e-{secrets.token_hex(4)}"
        self.namespace_created = False
        self.environment = os.environ | {"KUBECONFIG": str(directory / "kubeconfig")}
        self.credentials = {
            "S3_ACCESS_KEY": secrets.token_hex(10),
            "S3_SECRET_KEY": secrets.token_hex(20),
            "POSTGRES_PASSWORD": secrets.token_hex(20),
            "CLIENT_PASSWORD": secrets.token_hex(20),
            "CLIENT_TOKEN": secrets.token_hex(32),
        }

    def command(self, *command: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            command,
            check=False,
            input=input_text,
            text=True,
            capture_output=True,
            env=self.environment,
            timeout=900,
        )
        if result.returncode:
            raise RuntimeError(
                self.redact(
                    f"{' '.join(command)} failed:\n{result.stdout}\n{result.stderr}"
                )
            )
        return result.stdout

    def redact(self, content: str) -> str:
        for value in self.credentials.values():
            content = content.replace(value, "<ephemeral-test-credential>")
        return content

    def kubectl(self, *command: str) -> str:
        return self.command("kubectl", "-n", self.namespace, *command)

    def apply(self, *objects: dict) -> None:
        self.command(
            "kubectl",
            "-n",
            self.namespace,
            "apply",
            "-f",
            "-",
            input_text=json.dumps(
                {"apiVersion": "v1", "kind": "List", "items": objects}
            ),
        )

    def secret(self, name: str, data: dict[str, bytes]) -> None:
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name},
                "data": {
                    key: base64.b64encode(value).decode() for key, value in data.items()
                },
            }
        )

    def setup(self) -> None:
        print(
            f"Starting {self.arguments.catalog} E2E on k3s cluster {self.cluster}",
            flush=True,
        )
        if not self.arguments.cluster:
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
        storage_auth = {
            "identities": [
                {
                    "name": "e2e",
                    "credentials": [
                        {
                            "accessKey": self.credentials["S3_ACCESS_KEY"],
                            "secretKey": self.credentials["S3_SECRET_KEY"],
                        }
                    ],
                    "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                }
            ]
        }
        self.secret("storage-auth", {"s3.json": json.dumps(storage_auth).encode()})
        fixtures = ROOT / "test/kubernetes/fixtures"
        self.kubectl("apply", "-f", str(fixtures / "storage.yaml"))
        self.kubectl("apply", "-f", str(fixtures / f"{self.arguments.catalog}.yaml"))
        self.kubectl("rollout", "status", "deployment/object-store", "--timeout=180s")
        catalog = "postgres" if self.arguments.catalog == "ducklake" else "iceberg"
        self.kubectl("rollout", "status", f"deployment/{catalog}", "--timeout=180s")
        print("Catalog and S3 fixtures ready; seeding external lake data", flush=True)
        self.job("seed")
        print("Installing the chart with two query workers", flush=True)
        self.configure_workers()

    def configure_workers(self) -> None:
        certificate = self.directory / "server.crt"
        key = self.directory / "server.key"
        hostname = "lake-duckflight"
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
            f"/CN={hostname}",
            "-addext",
            f"subjectAltName=DNS:{hostname},DNS:{hostname}.{self.namespace}.svc,DNS:{hostname}.{self.namespace}.svc.cluster.local",
        )
        salt = secrets.token_bytes(16)
        verifier = hashlib.pbkdf2_hmac(
            "sha256", self.credentials["CLIENT_PASSWORD"].encode(), salt, 10000
        ).hex()
        token_digest = hashlib.sha256(
            self.credentials["CLIENT_TOKEN"].encode()
        ).hexdigest()
        auth = (
            f'[users.e2e]\npassword_hash="{verifier}"\nsalt={list(salt)}\niterations=10000\n'
            f'[tokens.reader]\nsha256="{token_digest}"\nsubject="reader"\n'
            'scopes=["query:execute", "transaction:manage"]\n'
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
        catalog = self.arguments.catalog
        init = (
            "install httpfs; load httpfs;\n"
            f"create secret storage (type s3, key_id '{self.credentials['S3_ACCESS_KEY']}', "
            f"secret '{self.credentials['S3_SECRET_KEY']}', endpoint 'object-store:8333', "
            f"url_style 'path', use_ssl false, scope 's3://warehouse/{catalog}/');\n"
        )
        if catalog == "ducklake":
            init += (
                "install postgres; load postgres; install ducklake; load ducklake;\n"
                "attach 'ducklake:postgres:host=postgres dbname=ducklake_catalog user=postgres "
                f"password={self.credentials['POSTGRES_PASSWORD']}' as lake "
                "(data_path 's3://warehouse/ducklake/', read_only);\n"
            )
        else:
            init += (
                "install iceberg; load iceberg;\n"
                "attach '' as lake "
                "(type iceberg, endpoint 'http://iceberg:8181', authorization_type 'none', read_only);\n"
            )
        init += f"set allowed_directories=['s3://warehouse/{catalog}/'];\n"
        init += "set max_temp_directory_size='256MiB';\n"
        self.secret("worker-bootstrap", {"init.sql": init.encode()})
        repository, tag = self.arguments.image.rsplit(":", 1)
        values = {
            "image": {"repository": repository, "tag": tag, "pullPolicy": "Never"},
            "auth": {"existingSecret": "worker-auth"},
            "bootstrap": {"existingSecret": "worker-bootstrap"},
            "duckdb": {"memoryLimit": "512MB", "threads": 1},
            "spill": {"sizeLimit": "512Mi"},
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

    def job(
        self, mode: str, pod_ip: str = "", service_account: str = "default"
    ) -> None:
        name = f"{mode}-{secrets.token_hex(3)}"
        self.apply(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": name},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 240,
                    "template": {
                        "spec": {
                            "serviceAccountName": service_account,
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "client",
                                    "image": self.client_image,
                                    "imagePullPolicy": "Never",
                                    "args": [mode],
                                    "envFrom": [
                                        {"secretRef": {"name": "test-credentials"}}
                                    ],
                                    "env": [
                                        {
                                            "name": "TEST_CATALOG",
                                            "value": self.arguments.catalog,
                                        },
                                        {
                                            "name": "TEST_HOST",
                                            "value": "lake-duckflight",
                                        },
                                        {"name": "TEST_POD_IP", "value": pod_ip},
                                    ],
                                    "volumeMounts": [
                                        {
                                            "name": "tls",
                                            "mountPath": "/run/test",
                                            "readOnly": True,
                                        }
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "tls",
                                    "secret": {
                                        "secretName": "client-tls",
                                        "optional": mode in ("seed", "append"),
                                    },
                                }
                            ],
                        }
                    },
                },
            }
        )
        deadline = time.monotonic() + 270
        while time.monotonic() < deadline:
            job = json.loads(self.kubectl("get", "job", name, "-o", "json"))
            status = job.get("status", {})
            if status.get("succeeded"):
                print(
                    self.redact(self.kubectl("logs", f"job/{name}")), end="", flush=True
                )
                return
            if status.get("failed"):
                raise AssertionError(self.redact(self.kubectl("logs", f"job/{name}")))
            time.sleep(2)
        raise TimeoutError(f"E2E client job {name} exceeded its deadline")

    def workers(self) -> list[dict]:
        return json.loads(
            self.kubectl(
                "get", "pods", "-l", "app.kubernetes.io/instance=lake", "-o", "json"
            )
        )["items"]

    def run(self) -> None:
        self.setup()
        workers = self.workers()
        assert len(workers) == 2
        self.job("verify")  # Service DNS and actual load-balancing path.
        for worker in workers:
            self.job("verify", worker["status"]["podIP"])
        self.job("append")  # External writer creates a new catalog snapshot.
        for worker in workers:
            self.job("verify-updated", worker["status"]["podIP"])
        previous_uids = {worker["metadata"]["uid"] for worker in workers}
        self.kubectl(
            "delete",
            "pod",
            workers[0]["metadata"]["name"],
            "--wait=true",
            "--timeout=120s",
        )
        self.kubectl(
            "rollout", "status", "deployment/lake-duckflight", "--timeout=300s"
        )
        replacements = self.workers()
        fresh = [
            pod for pod in replacements if pod["metadata"]["uid"] not in previous_uids
        ]
        assert len(replacements) == 2 and len(fresh) == 1
        self.job("verify-updated", fresh[0]["status"]["podIP"])
        self.job("verify-updated")
        assert not json.loads(self.kubectl("get", "pvc", "-o", "json"))["items"]
        print(
            f"PASS {self.arguments.catalog}: Helm install, both replicas/protocols, live catalog update, worker replacement/reconnect, no PVCs",
            flush=True,
        )

    def diagnose(self) -> None:
        self.arguments.artifacts.mkdir(parents=True, exist_ok=True)
        commands = {
            "pods.txt": ("get", "pods", "-o", "wide"),
            "events.txt": ("get", "events", "--sort-by=.lastTimestamp"),
            "workers.txt": (
                "logs",
                "-l",
                "app.kubernetes.io/instance=lake",
                "--all-containers",
                "--prefix",
                "--tail=100",
            ),
            "clients.txt": (
                "logs",
                "-l",
                "job-name",
                "--all-containers",
                "--prefix",
                "--tail=100",
            ),
            "catalog-storage.txt": (
                "logs",
                "-l",
                "fixture",
                "--all-containers",
                "--prefix",
                "--tail=100",
            ),
        }
        for filename, command in commands.items():
            try:
                output = self.kubectl(*command)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                output = str(error)
            (self.arguments.artifacts / filename).write_text(self.redact(output))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, choices=["iceberg", "ducklake"])
    parser.add_argument(
        "--image", required=True, help="Locally built or pulled server image tag"
    )
    parser.add_argument("--k3d", default="k3d")
    parser.add_argument(
        "--cluster", help="Reuse a disposable k3d cluster; do not delete it"
    )
    parser.add_argument(
        "--artifacts", type=Path, default=Path("/tmp/duckflight-e2e-artifacts")
    )
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="duckflight-k3s-") as directory:
        suite = Suite(arguments, Path(directory))
        try:
            suite.run()
        except Exception:
            suite.diagnose()
            raise
        finally:
            if arguments.cluster and suite.namespace_created:
                suite.kubectl("delete", "namespace", suite.namespace, "--wait=false")
            elif not arguments.cluster:
                suite.command(arguments.k3d, "cluster", "delete", suite.cluster)


if __name__ == "__main__":
    main()

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6,<7"]
# ///
"""Verify the chart's rendered workload and protocol/storage contracts."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "charts" / "duckflight"


def render(*settings: str, secrets: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "lake", str(CHART)]
    if secrets:
        settings = (
            "image.tag=e2e",
            "auth.existingSecret=auth",
            "bootstrap.existingSecret=bootstrap",
            *settings,
        )
    for setting in settings:
        command.extend(["--set", setting])
    return subprocess.run(command, text=True, capture_output=True, check=False)


class HelmChartTests(unittest.TestCase):
    def objects(self, *settings: str) -> dict:
        result = render(*settings)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {obj["kind"]: obj for obj in yaml.safe_load_all(result.stdout)}

    def test_stateless_workers_have_ephemeral_storage_and_restricted_secrets(self):
        objects = self.objects()
        self.assertEqual(set(objects), {"Deployment", "Service", "ServiceAccount"})
        deployment = objects["Deployment"]
        self.assertEqual(deployment["spec"]["replicas"], 2)
        pod = deployment["spec"]["template"]["spec"]
        worker = pod["containers"][0]
        environment = {item["name"]: item["value"] for item in worker["env"]}
        self.assertEqual(environment["DUCKFLIGHT_DATABASE"], ":memory:")
        self.assertEqual(environment["DUCKFLIGHT_TEMP_DIRECTORY"], "/data/spill")
        volumes = {item["name"]: item for item in pod["volumes"]}
        self.assertIn("emptyDir", volumes["data"])
        self.assertIn("emptyDir", volumes["tmp"])
        self.assertEqual(volumes["auth"]["secret"]["defaultMode"], 0o440)
        self.assertEqual(pod["securityContext"]["fsGroup"], 10001)
        self.assertTrue(worker["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(objects["Service"]["spec"]["type"], "ClusterIP")
        self.assertEqual(
            objects["Service"]["spec"]["selector"],
            deployment["spec"]["template"]["metadata"]["labels"],
        )

    def test_protocol_selection_keeps_service_environment_and_probe_consistent(self):
        cases = (
            (True, True, "flight"),
            (True, False, "postgres"),
            (False, True, "flight"),
        )
        for postgres, flight, probe in cases:
            with self.subTest(postgres=postgres, flight=flight):
                objects = self.objects(
                    f"postgres.enabled={str(postgres).lower()}",
                    f"flight.enabled={str(flight).lower()}",
                    "postgres.port=5544",
                    "flight.port=34444",
                )
                worker = objects["Deployment"]["spec"]["template"]["spec"][
                    "containers"
                ][0]
                enabled = set()
                environment = {item["name"]: item["value"] for item in worker["env"]}
                for protocol, active, port, variable in (
                    ("postgres", postgres, 5544, "DUCKFLIGHT_PG_ADDRESS"),
                    ("flight", flight, 34444, "DUCKFLIGHT_FLIGHT_ADDRESS"),
                ):
                    self.assertEqual(
                        environment[variable], f"0.0.0.0:{port}" if active else ""
                    )
                    if active:
                        enabled.add(protocol)
                self.assertEqual({p["name"] for p in worker["ports"]}, enabled)
                self.assertEqual(
                    {p["name"] for p in objects["Service"]["spec"]["ports"]}, enabled
                )
                for name in ("startupProbe", "readinessProbe"):
                    self.assertEqual(worker[name]["tcpSocket"]["port"], probe)

    def test_existing_service_account_and_digest(self):
        objects = self.objects(
            "serviceAccount.create=false",
            "serviceAccount.name=lake-identity",
            "automountServiceAccountToken=true",
            "image.digest=sha256:" + "a" * 64,
        )
        self.assertNotIn("ServiceAccount", objects)
        pod = objects["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "lake-identity")
        self.assertTrue(pod["automountServiceAccountToken"])
        self.assertEqual(
            pod["containers"][0]["image"],
            "ghcr.io/sidequery/duckflight-extension@sha256:" + "a" * 64,
        )

    def test_idle_mode_routes_both_protocols_through_one_controller(self):
        result = render("idleScale.enabled=true", "idleScale.existingSecret=idle")
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = {
            (obj["kind"], obj["metadata"]["name"]): obj
            for obj in yaml.safe_load_all(result.stdout)
        }
        worker = objects["Deployment", "lake-duckflight"]
        proxy = objects["Deployment", "lake-duckflight-idle-proxy"]
        self.assertEqual(worker["spec"]["replicas"], 0)
        self.assertEqual(proxy["spec"]["replicas"], 1)
        self.assertEqual(proxy["spec"]["strategy"], {"type": "Recreate"})
        pod = proxy["spec"]["template"]["spec"]
        container = pod["containers"][0]
        environment = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(environment["DUCKFLIGHT_PROXY_IDLE_TIMEOUT_SECONDS"], "3000")
        self.assertEqual(environment["DUCKFLIGHT_PROXY_POSTGRES_PORT"], "5433")
        self.assertEqual(environment["DUCKFLIGHT_PROXY_FLIGHT_PORT"], "31337")
        service = objects["Service", "lake-duckflight"]
        backend = objects["Service", "lake-duckflight-workers"]
        self.assertEqual(
            service["spec"]["selector"],
            proxy["spec"]["template"]["metadata"]["labels"],
        )
        self.assertEqual(
            backend["spec"]["selector"], worker["spec"]["selector"]["matchLabels"]
        )
        role = objects["Role", "lake-duckflight-idle-proxy"]
        self.assertEqual(role["rules"][0]["resourceNames"], ["lake-duckflight"])
        self.assertEqual(role["rules"][0]["verbs"], ["get", "patch"])
        self.assertEqual(role["rules"][1]["verbs"], ["list"])
        self.assertTrue(pod["automountServiceAccountToken"])
        worker_pod = worker["spec"]["template"]["spec"]
        self.assertFalse(worker_pod["automountServiceAccountToken"])
        worker_env = {
            item["name"]: item["value"] for item in worker_pod["containers"][0]["env"]
        }
        self.assertEqual(worker_env["DUCKFLIGHT_IDLE_EXPECTED_LISTENERS"], "2")
        policy = objects["NetworkPolicy", "lake-duckflight-workers"]["spec"]
        self.assertEqual(
            policy["podSelector"]["matchLabels"], backend["spec"]["selector"]
        )
        self.assertEqual(
            policy["ingress"][0]["from"][0]["podSelector"]["matchLabels"],
            service["spec"]["selector"],
        )
        self.assertEqual(
            {port["port"] for port in policy["ingress"][0]["ports"]},
            {5433, 31337, 4091},
        )

    def test_idle_mode_supports_either_protocol_independently(self):
        for disabled, remaining in (("postgres", "flight"), ("flight", "postgres")):
            with self.subTest(disabled=disabled):
                result = render(
                    "idleScale.enabled=true",
                    "idleScale.existingSecret=idle",
                    f"{disabled}.enabled=false",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                objects = list(yaml.safe_load_all(result.stdout))
                proxy = next(
                    obj
                    for obj in objects
                    if obj["kind"] == "Deployment"
                    and obj["metadata"]["name"].endswith("idle-proxy")
                )
                environment = {
                    item["name"]: item.get("value")
                    for item in proxy["spec"]["template"]["spec"]["containers"][0][
                        "env"
                    ]
                }
                self.assertNotIn(
                    f"DUCKFLIGHT_PROXY_{disabled.upper()}_PORT", environment
                )
                self.assertIn(f"DUCKFLIGHT_PROXY_{remaining.upper()}_PORT", environment)

    def test_invalid_runtime_configuration_fails_before_install(self):
        cases = (
            (("idleScale.enabled=true",), "idleScale.existingSecret"),
            (("idleScale.idleTimeoutSeconds=0",), "idleTimeoutSeconds"),
            (
                (
                    "idleScale.enabled=true",
                    "idleScale.existingSecret=idle",
                    "idleScale.controlPort=5433",
                ),
                "must differ",
            ),
            (
                (
                    "idleScale.enabled=true",
                    "idleScale.existingSecret=idle",
                    "flight.port=4090",
                ),
                "health port",
            ),
            (("postgres.enabled=false", "flight.enabled=false"), "at least one"),
            (("image.tag=",), "image.tag or image.digest"),
            (("replicaCount=-1",), "replicaCount"),
            (("flight.port=70000",), "port"),
            (("flight.port=5433",), "must differ"),
            (("serviceAccount.create=false",), "serviceAccount.name"),
            (
                ("extraEnv[0].name=DUCKFLIGHT_DATABASE", "extraEnv[0].value=/data/db"),
                "must not override",
            ),
        )
        for settings, message in cases:
            with self.subTest(settings=settings):
                result = render(*settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        result = render(secrets=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("auth.existingSecret", result.stderr)
        result = render("auth.existingSecret=auth", secrets=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap.existingSecret", result.stderr)


if __name__ == "__main__":
    unittest.main()

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

    def test_invalid_runtime_configuration_fails_before_install(self):
        cases = (
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

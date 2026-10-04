from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class BundledTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public = self.root / "public"
        self.public.mkdir()
        shutil.copy(REPO / "Makefile", self.public)
        for directory in (
            "scripts",
            "extension-ci-tools/makefiles",
            "extension-ci-tools/scripts",
        ):
            shutil.copytree(REPO / directory, self.public / directory)
        configure = self.public / "configure"
        (configure / "venv/bin").mkdir(parents=True)
        (configure / "venv/bin/python3").symlink_to(sys.executable)
        (configure / "extension_version.txt").write_text("test")
        self.platform_file = configure / "platform.txt"
        self.platform_file.write_text("osx_arm64")
        self.core = self.root / "core"
        manifest = self.core / "crates/duckflight-core-ffi/Cargo.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("# compiler substitute fixture\n")
        self.headers_helper = self.core / "scripts/prepare-extension-core-headers.sh"
        self.headers_helper.parent.mkdir()
        self.headers_helper.write_text(
            "#!/bin/bash\nset -eu\n"
            'printf "%s\\n" "$1" > "$HEADERS_LOG"\n'
            'headers="${DUCKFLIGHT_DUCKDB_INCLUDE_DIR:-$1/verified-headers}"\n'
            'mkdir -p "$headers"\n'
            'printf "verified fixture" > "$headers/duckdb.hpp"\n'
            'cd "$headers" && pwd -P\n'
        )
        self.tools = self.root / "bin"
        self.tools.mkdir()
        self.tool(
            "uname",
            '#!/bin/sh\ncase "$1" in -s) echo "$TEST_HOST_OS";; -m) echo "$TEST_HOST_ARCH";; esac\n',
        )
        self.tool("install_name_tool", "#!/bin/sh\nexit 0\n")
        # Only the compiler is replaced. Real Make targets copy its output and
        # the pinned CI helper appends actual DuckDB platform metadata.
        compiler = (
            f"#!{sys.executable}\n"
            + """import os
import pathlib
import sys
args = sys.argv[1:]
target = args[args.index("--target") + 1]
suffix = "dylib" if target.endswith("apple-darwin") else "so"
with open(os.environ["BUILD_LOG"], "a") as log:
    log.write(target + "\\n")
if "--manifest-path" in args:
    headers = pathlib.Path(os.environ["DUCKFLIGHT_DUCKDB_INCLUDE_DIR"])
    assert (headers / "duckdb.hpp").read_text() == "verified fixture"
    output = pathlib.Path(args[args.index("--target-dir") + 1]) / target / "release" / ("libduckflight_core_ffi." + suffix)
    data = ("core:" + target).encode()
else:
    output = pathlib.Path(os.environ.get("CARGO_TARGET_DIR", "target")) / target / "release" / ("libduckflight." + suffix)
    data = ("extension:" + target + "\\n").encode() + pathlib.Path(os.environ["DUCKFLIGHT_CORE_BUNDLE_PATH"]).read_bytes()
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(data)
"""
        )
        self.tool("cargo", compiler)
        self.tool("mbx", compiler)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DUCKDB_", "DUCKFLIGHT_", "CARGO_"))
            and key not in ("MAKEFLAGS", "MFLAGS", "OS")
        }
        self.env.update(
            PATH=f"{self.tools}:{os.environ['PATH']}",
            PYTHON_BIN=sys.executable,
            OPENSSL_DIR=str(self.root),
            BUILD_LOG=str(self.root / "build.log"),
            HEADERS_LOG=str(self.root / "headers.log"),
            CARGO_BUILD_TARGET="aarch64-apple-darwin",
            TEST_HOST_OS="Darwin",
            TEST_HOST_ARCH="arm64",
        )

    def tool(self, name: str, contents: str) -> None:
        path = self.tools / name
        path.write_text(contents)
        path.chmod(0o755)

    def run_build(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "scripts/build-bundled.sh", str(self.core)],
            cwd=self.public,
            env=self.env | overrides,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_build(self, platform: str, target: str, **overrides: str) -> None:
        result = self.run_build(**overrides)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (self.root / "build.log").read_text().splitlines(), [target, target]
        )
        artifact = (
            self.public / "build/release/duckflight.duckdb_extension"
        ).read_bytes()
        self.assertTrue(
            artifact.startswith(f"extension:{target}\ncore:{target}".encode())
        )
        # FIELD2 is followed by the 32-byte FIELD1 and 256-byte signature.
        self.assertEqual(artifact[-320:-288].rstrip(b"\0").decode(), platform)
        self.assertEqual(self.platform_file.read_text().strip(), platform)
        self.assertEqual(
            (self.root / "headers.log").read_text().strip(),
            overrides.get(
                "DUCKFLIGHT_CORE_TARGET_DIR",
                str(self.core / "target/duckflight-extension-bundle"),
            ),
        )

    def test_native(self) -> None:
        self.assert_build("osx_arm64", "aarch64-apple-darwin")

    def test_platform_override(self) -> None:
        self.assert_build(
            "osx_amd64", "x86_64-apple-darwin", DUCKDB_PLATFORM="osx_amd64"
        )

    def test_core_override(self) -> None:
        self.assert_build(
            "osx_amd64",
            "x86_64-apple-darwin",
            DUCKFLIGHT_CORE_TARGET="x86_64-apple-darwin",
        )

    def test_custom_target_directories(self) -> None:
        self.assert_build(
            "osx_arm64",
            "aarch64-apple-darwin",
            CARGO_TARGET_DIR=str(self.root / "extension-target"),
            DUCKFLIGHT_CORE_TARGET_DIR=str(self.root / "core-target"),
        )

    def test_native_linux(self) -> None:
        self.env.update(TEST_HOST_OS="Linux", TEST_HOST_ARCH="x86_64")
        self.platform_file.write_text("linux_amd64")
        self.assert_build("linux_amd64", "x86_64-unknown-linux-gnu")

    def test_explicit_header_directory(self) -> None:
        self.assert_build(
            "osx_arm64",
            "aarch64-apple-darwin",
            DUCKFLIGHT_DUCKDB_INCLUDE_DIR=str(self.root / "header override"),
        )

    def test_missing_header_helper_stops_before_compiling(self) -> None:
        self.headers_helper.unlink()
        result = self.run_build()
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "missing DuckFlight core header preparation helper", result.stderr
        )
        self.assertFalse((self.root / "build.log").exists())

    def test_failed_header_verification_stops_before_compiling(self) -> None:
        self.headers_helper.write_text('echo "header checksum mismatch" >&2\nexit 1\n')
        result = self.run_build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("header checksum mismatch", result.stderr)
        self.assertFalse((self.root / "build.log").exists())

    def test_linux_cross_architecture(self) -> None:
        self.env.update(TEST_HOST_OS="Linux", TEST_HOST_ARCH="x86_64")
        self.platform_file.write_text("linux_amd64")
        self.assert_build(
            "linux_arm64", "aarch64-unknown-linux-gnu", DUCKDB_PLATFORM="linux_arm64"
        )

    def test_matching_overrides_refresh_stale_configuration(self) -> None:
        self.platform_file.write_text("linux_arm64")
        self.assert_build(
            "osx_amd64",
            "x86_64-apple-darwin",
            DUCKDB_PLATFORM="osx_amd64",
            DUCKFLIGHT_CORE_TARGET="x86_64-apple-darwin",
        )

    def test_rejects_ambiguous_or_unsupported_targets_before_compiling(self) -> None:
        cases = [
            (
                {
                    "DUCKDB_PLATFORM": "osx_amd64",
                    "DUCKFLIGHT_CORE_TARGET": "aarch64-apple-darwin",
                },
                "conflicts",
            ),
            ({"DUCKDB_PLATFORM": "linux_arm64"}, "cross-OS"),
            ({"DUCKDB_PLATFORM": "linux_amd64_musl"}, "unsupported"),
            ({"DUCKFLIGHT_CORE_TARGET": "aarch64-unknown-linux-musl"}, "unsupported"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                result = self.run_build(**overrides)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.root / "build.log").exists())

    def test_stale_configuration_requires_explicit_selection(self) -> None:
        self.platform_file.write_text("osx_amd64")
        result = self.run_build()
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match host", result.stderr)
        self.assertFalse((self.root / "build.log").exists())


if __name__ == "__main__":
    unittest.main()

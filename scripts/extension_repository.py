#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Sign a native DuckFlight extension and prepare its public repository object."""

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_METADATA = (
    Path(__file__).resolve().parents[1]
    / "repository/.well-known/duckdb-extension-repo.json"
)
DEFAULT_KEY = "op://ja3rm4nzuu3bk5yi3rmumeetga/2eeiayq7xlbjj3y2tmaaqivkbm/private_key"
CHUNK_SIZE = 1024 * 1024
SIGNATURE_SIZE = 256


def run(arguments, data=None):
    """Capture subprocess output; never surface private-key inputs or diagnostics."""
    result = subprocess.run(arguments, input=data, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(
            f"{arguments[0]} {arguments[1]} failed (exit {result.returncode})"
        )
    return result.stdout


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
    ):
        raise ValueError("invalid repository path identifier")
    return value


def metadata(data, version):
    identifier(version)
    if len(data) <= 512 or data.startswith(b"\0asm"):
        raise ValueError("expected a native extension with a 512-byte footer")
    fields = []
    for offset in range(len(data) - 512, len(data) - 256, 32):
        raw = data[offset : offset + 32].rstrip(b"\0")
        if b"\0" in raw:
            raise ValueError("invalid metadata padding")
        try:
            fields.append(raw.decode("ascii"))
        except UnicodeDecodeError as error:
            raise ValueError("invalid metadata encoding") from error
    magic, platform, target, _extension_version, abi, *reserved = reversed(fields)
    if magic != "4" or any(reserved):
        raise ValueError("invalid metadata magic or reserved fields")
    identifier(platform)
    identifier(target)
    if abi not in ("", "CPP", "C_STRUCT", "C_STRUCT_UNSTABLE"):
        raise ValueError("unsupported extension ABI")
    if abi == "C_STRUCT":
        if not re.fullmatch(r"v[12]\.\d+\.\d+", target):
            raise ValueError("invalid stable C API version")
    elif target != version:
        raise ValueError("repository version must match the compiled DuckDB version")
    return platform


def digest(data):
    # DuckDB extension_load.cpp: ComputeFinalHash, InitializeAncillaryData.
    chunks = (
        hashlib.sha256(data[start : start + CHUNK_SIZE]).digest()
        for start in range(0, len(data), CHUNK_SIZE)
    )
    return hashlib.sha256(b"".join(chunks)).digest()


def verify(data, public_key):
    with tempfile.TemporaryDirectory(prefix="duckflight-verify-") as directory:
        directory = Path(directory)
        (directory / "public.pem").write_bytes(public_key)
        (directory / "digest").write_bytes(digest(data[:-SIGNATURE_SIZE]))
        (directory / "signature").write_bytes(data[-SIGNATURE_SIZE:])
        run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(directory / "public.pem"),
                "-in",
                str(directory / "digest"),
                "-sigfile",
                str(directory / "signature"),
                "-pkeyopt",
                "digest:sha256",
                "-pkeyopt",
                "rsa_padding_mode:pkcs1",
            ]
        )


def sign(data, private_key, public_key):
    if any(data[-SIGNATURE_SIZE:]):
        raise ValueError("refusing to replace an existing signature")
    actual_public = run(["openssl", "pkey", "-pubout", "-outform", "DER"], private_key)
    expected_public = run(["openssl", "pkey", "-pubin", "-outform", "DER"], public_key)
    if actual_public != expected_public:
        raise ValueError("private key does not match repository public key")
    with tempfile.TemporaryDirectory(prefix="duckflight-sign-") as directory:
        digest_file = Path(directory) / "digest"
        digest_file.write_bytes(digest(data[:-SIGNATURE_SIZE]))
        signature = run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                "/dev/stdin",
                "-in",
                str(digest_file),
                "-pkeyopt",
                "digest:sha256",
                "-pkeyopt",
                "rsa_padding_mode:pkcs1",
            ],
            private_key,
        )
    if len(signature) != SIGNATURE_SIZE:
        raise ValueError("signing requires an RSA-2048 key")
    signed = data[:-SIGNATURE_SIZE] + signature
    verify(signed, public_key)
    return signed


def prepare(
    artifact,
    output,
    version,
    public_metadata=DEFAULT_METADATA,
    key_reference=DEFAULT_KEY,
):
    if artifact.name != "duckflight.duckdb_extension":
        raise ValueError("artifact must be named duckflight.duckdb_extension")
    data = artifact.read_bytes()
    platform = metadata(data, version)
    if any(data[-SIGNATURE_SIZE:]):
        raise ValueError("refusing to replace an existing signature")
    document = json.loads(public_metadata.read_text())
    keys = document.get("signature_keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
        raise ValueError(
            "repository metadata must contain exactly one public signing key"
        )
    if not key_reference.startswith("op://"):
        raise ValueError("private key must be referenced through 1Password")
    private_key = run(["op", "read", key_reference])
    signed = sign(data, private_key, keys[0].encode())
    destination = output / version / platform / "duckflight.duckdb_extension.gz"
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as stream:
        stream.write(signed)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents silently replacing an already prepared release.
    with destination.open("xb") as stream:
        stream.write(compressed.getvalue())
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("artifact", type=Path)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument(
        "--version",
        required=True,
        help="runtime repository version; never changes artifact metadata",
    )
    command.add_argument("--public-metadata", type=Path, default=DEFAULT_METADATA)
    command.add_argument("--key-reference", default=DEFAULT_KEY)
    args = parser.parse_args()
    try:
        print(
            prepare(
                args.artifact,
                args.output,
                args.version,
                args.public_metadata,
                args.key_reference,
            )
        )
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

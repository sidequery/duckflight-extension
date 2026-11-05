#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=18,<24"]
# ///
"""Exercise HTTP and binary Quack through SQL functions in a real DuckDB host.

Run with a newly built runtime via DUCKFLIGHT_CORE_PATH and the debug extension,
or pass a bundled extension containing the same runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import secrets
import socket
import ssl
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pyarrow.ipc
from cli_host import CLIHost


def uleb(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def string(value: str) -> bytes:
    raw = value.encode()
    return uleb(len(raw)) + raw


def fields(values: dict[int, bytes]) -> bytes:
    return (
        b"".join(struct.pack("<H", key) + value for key, value in values.items())
        + b"\xff\xff"
    )


def quack_request(kind: int, connection: str | None, body: dict[int, bytes]) -> bytes:
    header = {1: uleb(kind)}
    if connection is not None:
        header[2] = string(connection)
    header[3] = uleb(1)
    return fields(header) + fields(body)


def quack_header(raw: bytes) -> tuple[int, str | None]:
    stream = io.BytesIO(raw)

    def read_uleb() -> int:
        result = 0
        for shift in range(0, 70, 7):
            byte = stream.read(1)
            assert byte, "truncated Quack response"
            result |= (byte[0] & 127) << shift
            if byte[0] < 128:
                return result
        raise AssertionError("invalid Quack integer")

    kind = None
    connection = None
    while True:
        field = struct.unpack("<H", stream.read(2))[0]
        if field == 65535:
            break
        if field == 1:
            kind = read_uleb()
        elif field == 2:
            connection = stream.read(read_uleb()).decode()
        elif field == 3:
            read_uleb()
        else:
            raise AssertionError(f"unexpected Quack header field {field}")
    assert kind is not None
    return kind, connection


def request(
    url: str, data: dict | bytes, token: str | None, context: ssl.SSLContext | None
):
    binary = isinstance(data, bytes)
    headers = {
        "Content-Type": "application/vnd.duckdb" if binary else "application/json"
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = data if binary else json.dumps(data).encode()
    req = urllib.request.Request(url, data=encoded, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def assert_closed(address: str) -> None:
    host, port = address.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            raise AssertionError(f"stopped listener remains reachable: {address}")
    except ConnectionRefusedError:
        pass


def run(cli: Path, extension: Path, tls: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="duckflight-http-quack-") as directory:
        fixtures = Path(directory)
        token = secrets.token_urlsafe(32)
        config = fixtures / "duckflight.toml"
        config.write_text(
            "shutdown_grace_secs = 1\n[tokens.smoke]\n"
            f'sha256 = "{hashlib.sha256(token.encode()).hexdigest()}"\n'
            'subject = "smoke"\n'
            'scopes = ["query:execute", "query:mutate", "transaction:manage"]\n'
        )
        config.chmod(0o600)
        context = None
        if tls:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(fixtures / "server.key"),
                    "-out",
                    str(fixtures / "server.crt"),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost,IP:127.0.0.1",
                ],
                check=True,
                capture_output=True,
            )
            with config.open("a") as file:
                file.write('\n[tls]\ncert = "server.crt"\nkey = "server.key"\n')
            context = ssl.create_default_context(cafile=str(fixtures / "server.crt"))

        with CLIHost(cli) as host:
            host.execute("load '" + str(extension.resolve()).replace("'", "''") + "'")
            assert host.execute(
                "select loaded from duckflight_core_status()"
            ).fetchone() == (True,)
            host.execute(
                "create table shared_rows(id integer); insert into shared_rows values (41)"
            )
            addresses = {}
            for protocol in ("http", "quack"):
                row = host.execute(
                    f"select * from duckflight_{protocol}_serve(?, ?)",
                    ["0.0.0.0:0" if tls else "127.0.0.1:0", str(config)],
                ).fetchone()
                assert row is not None and row[0] == protocol
                addresses[protocol] = row[1]
            assert host.execute(
                "select protocol from duckflight_servers() order by protocol"
            ).fetchall() == [("http",), ("quack",)]
            scheme = "https" if tls else "http"
            urls = {
                protocol: f"{scheme}://127.0.0.1:{address.rsplit(':', 1)[1]}"
                for protocol, address in addresses.items()
            }
            query = {
                "query": "select id from shared_rows order by id",
                "useCache": False,
            }
            for credential in (None, "incorrect"):
                status, _ = request(
                    urls["http"] + "/streaming-query", query, credential, context
                )
                assert status == 401, status
            status, raw = request(
                urls["http"] + "/streaming-query", query, token, context
            )
            assert status == 200, (status, raw)
            assert pyarrow.ipc.open_stream(raw).read_all().to_pylist() == [{"id": 41}]

            # Negotiate binary Quack v1, execute a mutation, and observe it from
            # both the SQL host and the independent HTTP listener.
            for credential, expected in (("incorrect", 100), (token, 2)):
                connect = quack_request(
                    1, None, {1: string(credential), 4: uleb(1), 5: uleb(1)}
                )
                status, raw = request(urls["quack"] + "/quack", connect, None, context)
                assert status == 200, (status, raw)
                kind, connection = quack_header(raw)
                assert kind == expected, (kind, raw)
            assert connection
            mutation = quack_request(
                3,
                connection,
                {1: string("insert into shared_rows values (42)"), 2: b"\x00\x01"},
            )
            status, raw = request(urls["quack"] + "/quack", mutation, None, context)
            assert status == 200 and quack_header(raw)[0] == 4, (status, raw)
            assert host.execute(
                "select id from shared_rows order by id"
            ).fetchall() == [(41,), (42,)]
            status, raw = request(
                urls["http"] + "/streaming-query", query, token, context
            )
            assert status == 200, (status, raw)
            assert pyarrow.ipc.open_stream(raw).read_all().to_pylist() == [
                {"id": 41},
                {"id": 42},
            ]

            # Leave an HTTP transaction and a Quack session alive while stopping.
            status, raw = request(urls["http"] + "/tx/begin", {}, token, context)
            assert status == 200, (status, raw)
            tx_id = json.loads(raw)["tx_id"]
            status, raw = request(
                urls["http"] + "/streaming-mutate",
                {
                    "operation": "insert",
                    "statement": "insert into shared_rows values (99)",
                    "txId": tx_id,
                },
                token,
                context,
            )
            assert status == 200, (status, raw)
            for protocol in ("http", "quack"):
                address = addresses[protocol]
                result = host.execute(
                    "select * from duckflight_stop(?, ?)", [protocol, address]
                ).fetchone()
                assert result == (f"stopped {protocol} server on {address}",)
                assert_closed("127.0.0.1:" + address.rsplit(":", 1)[1])
                if protocol == "http":
                    assert host.execute(
                        "select protocol from duckflight_servers()"
                    ).fetchall() == [("quack",)]
            assert host.execute("select * from duckflight_servers()").fetchall() == []
            assert host.execute(
                "select id from shared_rows order by id"
            ).fetchall() == [(41,), (42,)]
            # Each restart gets a fresh listener lifetime and can reuse its port.
            for protocol, address in addresses.items():
                assert host.execute(
                    f"select * from duckflight_{protocol}_serve(?, ?)",
                    [address, str(config)],
                ).fetchone() == (protocol, address)
            status, raw = request(urls["http"] + "/tx/begin", {}, token, context)
            assert status == 200, (status, raw)
            connect = quack_request(1, None, {1: string(token), 4: uleb(1), 5: uleb(1)})
            status, raw = request(urls["quack"] + "/quack", connect, None, context)
            assert status == 200 and quack_header(raw)[0] == 2, (status, raw)
            # Closing the host exercises runtime destruction with both listeners
            # and sessions alive, rather than relying only on explicit SQL stop.
        assert host.process.returncode == 0, "DuckDB host needed forced termination"
        print(
            f"PASS {'TLS' if tls else 'loopback'} HTTP IPC, Quack binary mutation, auth, shared database, stop and restart"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", type=Path)
    parser.add_argument("extension", type=Path)
    args = parser.parse_args()
    run(args.cli, args.extension, tls=False)
    run(args.cli, args.extension, tls=True)

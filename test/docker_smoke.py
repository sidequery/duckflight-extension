#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2,<4", "pyarrow>=18,<24"]
# ///
"""Exercise the shipped container through real TLS PostgreSQL and Flight clients."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
from pyarrow import flight


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def protobuf_strings(fields: dict[int, bytes]) -> bytes:
    encoded = bytearray()
    for tag, value in fields.items():
        encoded.append((tag << 3) | 2)
        length = len(value)
        while length >= 128:
            encoded.append((length & 127) | 128)
            length >>= 7
        encoded.append(length)
        encoded.extend(value)
    return bytes(encoded)


def run(image: str, startup_timeout: float) -> None:
    password = secrets.token_urlsafe(24)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 10000).hex()
    volume = f"duckflight-smoke-{secrets.token_hex(6)}"
    containers: list[str] = []
    with tempfile.TemporaryDirectory(prefix="duckflight-docker-") as directory:
        fixtures = Path(directory)
        fixtures.chmod(0o755)
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
        users = (
            f'[users.smoke]\npassword_hash = "{digest}"\n'
            f"salt = {list(salt)}\niterations = 10000\n"
        )
        (fixtures / "plaintext.toml").write_text(users)
        (fixtures / "duckflight.toml").write_text(
            users + '\n[tls]\ncert = "server.crt"\nkey = "server.key"\n'
        )
        (fixtures / "init.sql").write_text(
            "create table if not exists smoke_rows(id integer);"
        )
        (fixtures / "invalid.sql").write_text("this is not valid sql;")
        (fixtures / "quote's.toml").write_text(
            (fixtures / "duckflight.toml").read_text()
        )
        (fixtures / "quote's.sql").write_text(
            "create table initialized(value varchar);"
            "insert into initialized values ('ready') -- no final semicolon"
        )
        # Ephemeral test credentials only; readable by the image's non-root UID.
        for path in fixtures.iterdir():
            path.chmod(0o644)

        def start(*env: str) -> str:
            args = [
                "run",
                "-d",
                "--mount",
                f"type=volume,src={volume},dst=/data",
                "--mount",
                f"type=bind,src={fixtures},dst=/run/secrets,readonly",
                "-p",
                "127.0.0.1::4543",
                "-p",
                "127.0.0.1::45337",
                "-e",
                "DUCKFLIGHT_PG_ADDRESS=0.0.0.0:4543",
                "-e",
                "DUCKFLIGHT_FLIGHT_ADDRESS=0.0.0.0:45337",
                "-e",
                "DUCKFLIGHT_THREADS=2",
                "-e",
                "DUCKFLIGHT_MEMORY_LIMIT=256MB",
                "-e",
                "DUCKFLIGHT_INIT_SQL=/run/secrets/init.sql",
            ]
            for value in env:
                args.extend(["-e", value])
            container = docker(*args, image)
            containers.append(container)
            return container

        def ready(container: str) -> None:
            began = time.monotonic()
            deadline = began + startup_timeout
            while time.monotonic() < deadline:
                if "DuckFlight ready" in docker("logs", container):
                    print(
                        f"Container ready after {time.monotonic() - began:.1f}s",
                        flush=True,
                    )
                    return
                state = json.loads(docker("inspect", container))[0]["State"]
                if not state["Running"]:
                    raise AssertionError(docker("logs", container))
                time.sleep(0.2)
            raise AssertionError(
                f"container did not become ready within {startup_timeout}s:\n"
                + docker("logs", container)
            )

        def pg(container: str, supplied_password: str = password):
            port = int(docker("port", container, "4543/tcp").rsplit(":", 1)[1])
            return psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="smoke",
                password=supplied_password,
                dbname="duckflight",
                sslmode="verify-full",
                sslrootcert=str(fixtures / "server.crt"),
                connect_timeout=5,
                autocommit=True,
            )

        def stop(container: str) -> None:
            docker("stop", "--time", "10", container)
            assert docker("inspect", "-f", "{{.State.ExitCode}}", container) == "0"

        try:
            container = start()
            ready(container)
            with pg(container) as connection:
                threads = connection.execute(
                    "select current_setting('threads')::integer"
                ).fetchone()
                assert threads == (2,), threads
                connection.execute("insert into smoke_rows values (42)")
            try:
                pg(container, "incorrect")
            except psycopg.OperationalError:
                pass
            else:
                raise AssertionError("incorrect PostgreSQL password accepted")
            port = int(docker("port", container, "45337/tcp").rsplit(":", 1)[1])
            with flight.FlightClient(
                f"grpc+tls://localhost:{port}",
                tls_root_certs=(fixtures / "server.crt").read_bytes(),
            ) as client:
                token = client.authenticate_basic_token("smoke", password)
                options = flight.FlightCallOptions(headers=[token], timeout=10)
                command = protobuf_strings(
                    {
                        1: b"type.googleapis.com/arrow.flight.protocol.sql.CommandStatementQuery",
                        2: protobuf_strings({1: b"select id from smoke_rows"}),
                    }
                )
                info = client.get_flight_info(
                    flight.FlightDescriptor.for_command(command), options
                )
                result = client.do_get(info.endpoints[0].ticket, options).read_all()
                assert result.to_pylist() == [{"id": 42}]
                try:
                    client.authenticate_basic_token("smoke", "incorrect")
                except flight.FlightUnauthenticatedError:
                    pass
                else:
                    raise AssertionError("incorrect Flight password accepted")
            stop(container)
            container = start("DUCKFLIGHT_FLIGHT_ADDRESS=")
            ready(container)
            with pg(container) as connection:
                assert connection.execute("select id from smoke_rows").fetchall() == [
                    (42,)
                ]
                assert connection.execute(
                    "select protocol from duckflight_servers()"
                ).fetchall() == [("pgwire",)]
            stop(container)
            container = start("DUCKFLIGHT_PG_ADDRESS=", "DUCKFLIGHT_DATABASE=:memory:")
            ready(container)
            assert "pgwire listening" not in docker("logs", container)
            stop(container)
            container = start(
                "DUCKFLIGHT_FLIGHT_ADDRESS=",
                "DUCKFLIGHT_DATABASE=:memory:",
                "DUCKFLIGHT_CONFIG=/run/secrets/quote's.toml",
                "DUCKFLIGHT_INIT_SQL=/run/secrets/quote's.sql",
                "DUCKFLIGHT_TEMP_DIRECTORY=/data/spill's",
            )
            ready(container)
            with pg(container) as connection:
                assert connection.execute("select * from initialized").fetchall() == [
                    ("ready",)
                ]
                assert connection.execute(
                    "select current_setting('temp_directory')"
                ).fetchone() == ("/data/spill's",)
            docker("kill", "--signal", "SIGINT", container)
            exited = subprocess.run(
                ["docker", "wait", container],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert exited.stdout.strip() == "0", exited.stdout
            for env in (
                ("DUCKFLIGHT_CONFIG=/missing.toml",),
                ("DUCKFLIGHT_CONFIG=/run/secrets/plaintext.toml",),
                ("DUCKFLIGHT_PG_ADDRESS=", "DUCKFLIGHT_FLIGHT_ADDRESS="),
                ("DUCKFLIGHT_THREADS=invalid",),
                ("DUCKFLIGHT_INIT_SQL=/missing.sql",),
                ("DUCKFLIGHT_INIT_SQL=/run/secrets/invalid.sql",),
                # Second-listener failure must clean up the first one too.
                ("DUCKFLIGHT_FLIGHT_ADDRESS=invalid",),
            ):
                container = start(*env)
                process = subprocess.run(
                    ["docker", "wait", container],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert process.returncode == 0 and process.stdout.strip() != "0", env
            assert "v1.5.6" in docker(
                "run", "--rm", "--entrypoint", "duckdb", image, "--version"
            )
            print(
                "PASS TLS PostgreSQL/Flight, auth rejection, resource knobs, protocol selection,"
            )
            print("     persistence, graceful stop, startup failures, and DuckDB CLI")
        finally:
            for container in containers:
                docker("rm", "-f", container)
            docker("volume", "rm", volume)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="locally built image tag")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30,
        help="readiness deadline in seconds (increase for CPU emulation)",
    )
    args = parser.parse_args()
    run(args.image, args.startup_timeout)

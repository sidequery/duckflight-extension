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
import pyarrow as pa
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


def flight_query(client, options, sql: str):
    command = protobuf_strings(
        {
            1: b"type.googleapis.com/arrow.flight.protocol.sql.CommandStatementQuery",
            2: protobuf_strings({1: sql.encode()}),
        }
    )
    info = client.get_flight_info(flight.FlightDescriptor.for_command(command), options)
    return client.do_get(info.endpoints[0].ticket, options).read_all().to_pylist()


def flight_execute(client, options, sql: str) -> None:
    command = protobuf_strings(
        {
            1: b"type.googleapis.com/arrow.flight.protocol.sql.CommandStatementUpdate",
            2: protobuf_strings({1: sql.encode()}),
        }
    )
    writer, reader = client.do_put(
        flight.FlightDescriptor.for_command(command), pa.schema([]), options
    )
    try:
        writer.done_writing()
        reader.read()
    finally:
        writer.close()


SECRET_QUERIES = (
    "select content from read_text('/run/secrets/duckflight.toml')",
    "select content from read_text('/run/secrets/server.key')",
    "select content from read_blob('/run/secrets/server.key')",
    "select content from read_text('/imports/../run/secrets/server.key')",
    "select content from read_text('/imports/key-alias')",
    (
        "select * from read_csv('/run/secrets/server.key', header=false, "
        "columns={'line':'varchar'}, delim='|')"
    ),
)

CONFIG_BYPASSES = (
    "set enable_external_access=true",
    "set lock_configuration=false",
    "set allowed_paths=['/run/secrets/server.key']",
    "set allowed_directories=['/run/secrets']",
    "set allowed_configs=['enable_external_access','lock_configuration']",
)

CONFIG_RESETS = (
    "reset enable_external_access",
    "reset lock_configuration",
    "reset allowed_paths",
    "reset allowed_directories",
)


def denied(execute, sql: str, error_type, message: str) -> None:
    try:
        execute(sql)
    except error_type as error:
        assert message in str(error).lower(), (sql, str(error))
    else:
        raise AssertionError(f"restricted SQL succeeded: {sql}")


def pg_security(connection) -> None:
    for sql in SECRET_QUERIES:
        denied(connection.execute, sql, psycopg.Error, "permission")
    for sql in CONFIG_BYPASSES:
        denied(connection.execute, sql, psycopg.Error, "configuration")
    denied(
        connection.execute,
        "load '/opt/duckflight/duckflight.duckdb_extension'",
        psycopg.Error,
        "not allowed",
    )
    for sql in CONFIG_RESETS:
        # PgWire may acknowledge RESET as a compatibility no-op. Neither success
        # nor an error may relax the shared database policy.
        try:
            connection.execute(sql)
        except psycopg.Error as error:
            assert "configuration" in str(error).lower(), (sql, str(error))
        policy = connection.execute(
            "select current_setting('enable_external_access')::boolean, "
            "current_setting('lock_configuration')::boolean"
        ).fetchone()
        assert policy == (False, True), (sql, policy)
        denied(connection.execute, SECRET_QUERIES[1], psycopg.Error, "permission")


def run(image: str, startup_timeout: float) -> None:
    password = secrets.token_urlsafe(24)
    read_token = secrets.token_urlsafe(24)
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
            users
            + f'\n[tokens.reader]\nsha256 = "{hashlib.sha256(read_token.encode()).hexdigest()}"\n'
            + 'subject = "reader"\nscopes = ["query:execute", "transaction:manage"]\n'
            + '\n[tls]\ncert = "server.crt"\nkey = "server.key"\n'
        )
        (fixtures / "bootstrap.csv").write_text("value\nready\n")
        (fixtures / "init.sql").write_text(
            "create table if not exists smoke_rows(id integer);"
            "create or replace table startup_import as "
            "select * from read_csv('/run/secrets/bootstrap.csv');"
        )
        imports = fixtures / "imports"
        imports.mkdir(mode=0o755)
        (imports / "input.csv").write_text("id\n7\n")
        (imports / "key-alias").symlink_to("/run/secrets/server.key")
        (imports / "export").mkdir(mode=0o777)
        (imports / "export").chmod(0o777)
        (fixtures / "allowlist.sql").write_text(
            (fixtures / "init.sql").read_text()
            + "set allowed_paths=['/imports/input.csv'];"
            + "set allowed_directories=['/imports/export'];"
            + "set allowed_configs=['enable_external_access'];"
        )
        (fixtures / "invalid.sql").write_text("this is not valid sql;")
        (fixtures / "prelocked.sql").write_text("set lock_configuration=true;")
        (fixtures / "quote's.toml").write_text(
            (fixtures / "duckflight.toml").read_text()
        )
        (fixtures / "quote's.sql").write_text(
            "create table initialized(value varchar);"
            "insert into initialized values ('ready') -- no final semicolon"
        )
        # Ephemeral test credentials only; readable by the image's non-root UID.
        for path in fixtures.iterdir():
            if path.is_file():
                path.chmod(0o644)

        def start(*env: str) -> str:
            args = [
                "run",
                "-d",
                "--mount",
                f"type=volume,src={volume},dst=/data",
                "--mount",
                f"type=bind,src={fixtures},dst=/run/secrets,readonly",
                "--mount",
                f"type=bind,src={imports},dst=/imports",
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

        def cancel_startup(signal: str, large_input: bool) -> None:
            marker = f"/data/running-{signal}-{int(large_input)}.csv"
            startup = fixtures / "long-startup.sql"
            startup.write_text(
                "copy (select sum(i) from range(1000000000000) t(i)) "
                f"to '{marker}';\n"
                # The CLI cannot consume this input while COPY is executing.
                + ("-- queued startup input\n" * 100000 if large_input else "")
                + "create table startup_finished(value integer);\n"
            )
            startup.chmod(0o644)
            container = start("DUCKFLIGHT_INIT_SQL=/run/secrets/long-startup.sql")
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                # COPY creates its output sink during execution, before the
                # aggregate finishes: this proves the long query has begun.
                if (
                    docker(
                        "exec",
                        container,
                        "duckdb",
                        "-noheader",
                        "-list",
                        ":memory:",
                        "-c",
                        f"select count(*) from glob('{marker}')",
                    )
                    == "1"
                ):
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("long startup query did not begin")
            began = time.monotonic()
            docker("kill", "--signal", signal, container)
            exited = subprocess.run(
                ["docker", "wait", container],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            state = json.loads(docker("inspect", container))[0]["State"]
            assert exited.stdout.strip() in ("0", "1", "130"), state
            assert not state["OOMKilled"], state
            logs = docker("logs", container)
            assert "DuckFlight ready" not in logs, logs
            assert "listening on" not in logs, logs
            print(
                f"Cancelled {signal} startup (large input={large_input}) "
                f"after query began in {time.monotonic() - began:.1f}s",
                flush=True,
            )

        try:
            container = start()
            ready(container)
            with pg(container) as connection:
                threads = connection.execute(
                    "select current_setting('threads')::integer"
                ).fetchone()
                assert threads == (2,), threads
                connection.execute("set timezone='UTC'")
                assert connection.execute(
                    "select current_setting('TimeZone')"
                ).fetchone() == ("UTC",)
                assert connection.execute(
                    "select * from startup_import"
                ).fetchall() == [("ready",)]
                connection.execute("insert into smoke_rows values (42)")
                pg_security(connection)
                assert connection.execute("select id from smoke_rows").fetchall() == [
                    (42,)
                ]
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
                assert flight_query(client, options, "select id from smoke_rows") == [
                    {"id": 42}
                ]
                flight_execute(client, options, "insert into smoke_rows values (43)")
                flight_execute(
                    client, options, "update smoke_rows set id=44 where id=43"
                )
                assert flight_query(
                    client, options, "select id from smoke_rows order by id"
                ) == [{"id": 42}, {"id": 44}]
                flight_execute(client, options, "delete from smoke_rows where id=44")
                for sql in SECRET_QUERIES:
                    denied(
                        lambda sql: flight_query(client, options, sql),
                        sql,
                        flight.FlightError,
                        "permission",
                    )
                for sql in CONFIG_BYPASSES + CONFIG_RESETS:
                    denied(
                        lambda sql: flight_execute(client, options, sql),
                        sql,
                        flight.FlightError,
                        "configuration",
                    )
                read_options = flight.FlightCallOptions(
                    headers=[(b"authorization", f"Bearer {read_token}".encode())],
                    timeout=10,
                )
                for sql in SECRET_QUERIES:
                    denied(
                        lambda sql: flight_query(client, read_options, sql),
                        sql,
                        flight.FlightError,
                        "permission",
                    )
                assert flight_query(
                    client, read_options, "select id from smoke_rows"
                ) == [{"id": 42}]
                try:
                    client.authenticate_basic_token("smoke", "incorrect")
                except flight.FlightUnauthenticatedError:
                    pass
                else:
                    raise AssertionError("incorrect Flight password accepted")
            stop(container)
            container = start("DUCKFLIGHT_INIT_SQL=/run/secrets/allowlist.sql")
            ready(container)
            with pg(container) as connection:
                assert connection.execute(
                    "select id from read_csv('/imports/input.csv')"
                ).fetchall() == [(7,)]
                connection.execute(
                    "copy smoke_rows to '/imports/export/rows.csv' (header)"
                )
                assert connection.execute(
                    "select id from read_csv('/imports/export/rows.csv')"
                ).fetchall() == [(42,)]
                pg_security(connection)
            port = int(docker("port", container, "45337/tcp").rsplit(":", 1)[1])
            with flight.FlightClient(
                f"grpc+tls://localhost:{port}",
                tls_root_certs=(fixtures / "server.crt").read_bytes(),
            ) as client:
                token = client.authenticate_basic_token("smoke", password)
                options = flight.FlightCallOptions(headers=[token], timeout=10)
                assert flight_query(
                    client, options, "select id from read_csv('/imports/input.csv')"
                ) == [{"id": 7}]
                denied(
                    lambda sql: flight_query(client, options, sql),
                    SECRET_QUERIES[1],
                    flight.FlightError,
                    "permission",
                )
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
            for signal in ("SIGTERM", "SIGINT"):
                for large_input in (False, True):
                    cancel_startup(signal, large_input)
            container = start()
            ready(container)
            with pg(container) as connection:
                assert connection.execute("select id from smoke_rows").fetchall() == [
                    (42,)
                ]
                assert connection.execute(
                    "select count(*) from information_schema.tables "
                    "where table_name = 'startup_finished'"
                ).fetchone() == (0,)
            stop(container)
            for env in (
                ("DUCKFLIGHT_CONFIG=/missing.toml",),
                ("DUCKFLIGHT_CONFIG=/run/secrets/plaintext.toml",),
                ("DUCKFLIGHT_PG_ADDRESS=", "DUCKFLIGHT_FLIGHT_ADDRESS="),
                ("DUCKFLIGHT_THREADS=invalid",),
                ("DUCKFLIGHT_INIT_SQL=/missing.sql",),
                ("DUCKFLIGHT_INIT_SQL=/run/secrets/invalid.sql",),
                ("DUCKFLIGHT_INIT_SQL=/run/secrets/prelocked.sql",),
                # Second-listener failure must clean up the first one too.
                ("DUCKFLIGHT_FLIGHT_ADDRESS=invalid",),
            ):
                container = start(*env)
                try:
                    process = subprocess.run(
                        ["docker", "wait", container],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except subprocess.TimeoutExpired as error:
                    raise AssertionError(
                        f"startup failure did not exit for {env}:\n"
                        + docker("logs", container)
                    ) from error
                assert process.returncode == 0 and process.stdout.strip() != "0", env
                if env == ("DUCKFLIGHT_INIT_SQL=/run/secrets/prelocked.sql",):
                    logs = subprocess.check_output(
                        ["docker", "logs", container],
                        text=True,
                        stderr=subprocess.STDOUT,
                    )
                    assert "configuration has been locked" in logs, logs
                    assert "listening on" not in logs, logs
                    assert "DuckFlight ready" not in logs, logs
            assert "v1.5.6" in docker(
                "run", "--rm", "--entrypoint", "duckdb", image, "--version"
            )
            print(
                "PASS TLS PostgreSQL/Flight, auth rejection, resource knobs, protocol selection,"
            )
            print(
                "     persistence, graceful stop, startup cancellation/failures, and DuckDB CLI"
            )
            print(
                "PASS client secrets isolation, locked settings, and operator data allowlists"
            )
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

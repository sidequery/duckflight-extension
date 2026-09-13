#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "psycopg[binary]==3.3.5",
#   "adbc-driver-flightsql==1.12.0",
#   "pyarrow==25.0.1",
# ]
# ///
"""Real DuckFlight alpha regression, isolated in a child with a default 120s deadline.

Set DUCKDB_ALPHA_LIBRARY and DUCKFLIGHT_ALPHA_EXTENSION, then run with uv.
Unsigned local artifacts additionally require --allow-unsigned. --repository PREFIX
installs from a signed repository instead and always requires signature validation.
Unittest discovery skips this integration test when no alpha library is configured.
DUCKFLIGHT_ALPHA_TEST_TIMEOUT overrides the child deadline on overloaded hosts.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class DuckDBResult(ctypes.Structure):
    # Public duckdb.h v1 result layout; values are read through accessor functions.
    _fields_ = [
        ("deprecated_column_count", ctypes.c_uint64),
        ("deprecated_row_count", ctypes.c_uint64),
        ("deprecated_rows_changed", ctypes.c_uint64),
        ("deprecated_columns", ctypes.c_void_p),
        ("deprecated_error_message", ctypes.c_void_p),
        ("internal_data", ctypes.c_void_p),
    ]


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def require_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


class Host:
    def __init__(self, library: str, directory: Path, unsigned: bool):
        self.api = ctypes.CDLL(library)
        pointer = ctypes.c_void_p
        signatures = {
            "duckdb_create_config": ([ctypes.POINTER(pointer)], ctypes.c_int),
            "duckdb_set_config": (
                [pointer, ctypes.c_char_p, ctypes.c_char_p],
                ctypes.c_int,
            ),
            "duckdb_destroy_config": ([ctypes.POINTER(pointer)], None),
            "duckdb_open_ext": (
                [
                    ctypes.c_char_p,
                    ctypes.POINTER(pointer),
                    pointer,
                    ctypes.POINTER(pointer),
                ],
                ctypes.c_int,
            ),
            "duckdb_close": ([ctypes.POINTER(pointer)], None),
            "duckdb_connect": ([pointer, ctypes.POINTER(pointer)], ctypes.c_int),
            "duckdb_disconnect": ([ctypes.POINTER(pointer)], None),
            "duckdb_query": (
                [pointer, ctypes.c_char_p, ctypes.POINTER(DuckDBResult)],
                ctypes.c_int,
            ),
            "duckdb_destroy_result": ([ctypes.POINTER(DuckDBResult)], None),
            "duckdb_result_error": ([ctypes.POINTER(DuckDBResult)], ctypes.c_char_p),
            "duckdb_value_varchar": (
                [ctypes.POINTER(DuckDBResult), ctypes.c_uint64, ctypes.c_uint64],
                pointer,
            ),
            "duckdb_row_count": ([ctypes.POINTER(DuckDBResult)], ctypes.c_uint64),
            "duckdb_column_count": ([ctypes.POINTER(DuckDBResult)], ctypes.c_uint64),
            "duckdb_free": ([pointer], None),
        }
        for name, (arguments, result) in signatures.items():
            getattr(self.api, name).argtypes = arguments
            getattr(self.api, name).restype = result
        self.database, self.connection = pointer(), pointer()
        config, error = pointer(), pointer()
        try:
            require_equal(
                self.api.duckdb_create_config(ctypes.byref(config)), 0, "create config"
            )
            for name, value in {
                "extension_repository_directory": str(directory / "trust"),
                "extension_directories": f"[{sql_literal(str(directory / 'installed'))}]",
                "allow_unsigned_extensions": str(unsigned).lower(),
                "allow_extension_repositories": "allowed",
            }.items():
                require_equal(
                    self.api.duckdb_set_config(config, name.encode(), value.encode()),
                    0,
                    name,
                )
            status = self.api.duckdb_open_ext(
                None, ctypes.byref(self.database), config, ctypes.byref(error)
            )
            if status:
                raise RuntimeError(
                    ctypes.string_at(error).decode() if error else "open failed"
                )
            require_equal(
                self.api.duckdb_connect(self.database, ctypes.byref(self.connection)),
                0,
                "connect",
            )
        except BaseException:
            self.close()
            raise
        finally:
            self.api.duckdb_free(error)
            self.api.duckdb_destroy_config(ctypes.byref(config))

    def query(self, sql: str):
        result = DuckDBResult()
        try:
            if self.api.duckdb_query(
                self.connection, sql.encode(), ctypes.byref(result)
            ):
                raise RuntimeError(
                    (
                        self.api.duckdb_result_error(ctypes.byref(result))
                        or b"query failed"
                    ).decode()
                )
            rows = []
            for row in range(self.api.duckdb_row_count(ctypes.byref(result))):
                values = []
                for column in range(self.api.duckdb_column_count(ctypes.byref(result))):
                    value = self.api.duckdb_value_varchar(
                        ctypes.byref(result), column, row
                    )
                    try:
                        values.append(
                            ctypes.string_at(value).decode() if value else None
                        )
                    finally:
                        self.api.duckdb_free(value)
                rows.append(tuple(values))
            return rows
        finally:
            self.api.duckdb_destroy_result(ctypes.byref(result))

    def close(self):
        if self.connection:
            self.api.duckdb_disconnect(ctypes.byref(self.connection))
        if self.database:
            self.api.duckdb_close(ctypes.byref(self.database))


def run_clients(repository: str | None, unsigned: bool):
    import adbc_driver_flightsql.dbapi as flightsql
    import psycopg
    from pyarrow import flight

    password = secrets.token_urlsafe(24)
    username = "alpha_regression"
    with tempfile.TemporaryDirectory(prefix="duckflight-alpha-clients-") as directory:
        root = Path(directory)
        salt = secrets.token_bytes(16)
        # Same PBKDF2-SHA256 verifier contract as scripts/duckflight_auth.py.
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, 10_000, dklen=32
        ).hex()
        config = root / "auth.toml"
        config.write_text(
            f'[users.{username}]\npassword_hash = "{digest}"\nsalt = {list(salt)}\niterations = 10000\n'
        )
        config.chmod(0o600)
        host = Host(os.environ["DUCKDB_ALPHA_LIBRARY"], root, unsigned)
        servers = []
        try:
            require_equal(
                host.query("select current_setting('allow_unsigned_extensions')"),
                [(str(unsigned).lower(),)],
                "signature policy",
            )
            version = host.query("select version()")
            if not version[0][0].startswith("v2.0.0"):
                raise AssertionError(f"expected DuckDB 2.0 alpha: {version}")
            print(f"Loading DuckFlight on {version[0][0]}", flush=True)
            started = time.monotonic()
            if repository:
                host.query(
                    f"CREATE EXTENSION REPOSITORY alpha_test WITH PREFIX {sql_literal(repository)}"
                )
                print(
                    f"Repository trusted in {time.monotonic() - started:.1f}s",
                    flush=True,
                )
                host.query("INSTALL duckflight FROM alpha_test")
                print(
                    f"Signed artifact installed in {time.monotonic() - started:.1f}s",
                    flush=True,
                )
                host.query("LOAD duckflight FROM alpha_test")
            else:
                artifact = Path(os.environ["DUCKFLIGHT_ALPHA_EXTENSION"]).resolve(
                    strict=True
                )
                host.query(f"LOAD {sql_literal(str(artifact))}")
            print(f"Extension loaded in {time.monotonic() - started:.1f}s", flush=True)
            require_equal(
                host.query("select loaded, abi_version from duckflight_core_status()"),
                [("true", "1")],
                "real bundled core initialized",
            )
            print(f"PASS actual core initialized on {version[0][0]}", flush=True)
            host.query(
                "create table alpha_shared(id integer primary key, label varchar); insert into alpha_shared values (1, 'host')"
            )
            for protocol, function in [
                ("pgwire", "duckflight_pg_serve"),
                ("flight", "duckflight_flight_serve"),
            ]:
                address = host.query(
                    f"select address from {function}('127.0.0.1:0', {sql_literal(str(config))})"
                )[0][0]
                servers.append((protocol, address))
            require_equal(
                sorted(
                    host.query("select protocol, address from duckflight_servers()")
                ),
                sorted(servers),
                "listener inventory",
            )
            pg_address, flight_address = servers[0][1], servers[1][1]
            pg_host, pg_port = pg_address.rsplit(":", 1)
            pg_options = {
                "host": pg_host,
                "port": int(pg_port),
                "user": username,
                "password": password,
                "dbname": "duckflight",
                "connect_timeout": 5,
                "autocommit": True,
            }
            with psycopg.connect(**pg_options) as connection:
                require_equal(
                    connection.execute("select * from alpha_shared").fetchall(),
                    [(1, "host")],
                    "PgWire shared host table",
                )
                require_equal(
                    connection.execute(
                        "select initcap('hELLO wORLD'), array_remove([1,2,1],1), array_replace([1,2,1],1,3)"
                    ).fetchone(),
                    ("Hello World", [2], [3, 2, 3]),
                    "PgWire compatibility macros",
                )
                connection.execute("begin")
                connection.execute("insert into alpha_shared values (2, 'pgwire')")
                connection.execute("commit")
                connection.execute("begin")
                connection.execute("insert into alpha_shared values (3, 'rollback')")
                connection.execute("rollback")
            require_equal(
                host.query("select * from alpha_shared order by id"),
                [("1", "host"), ("2", "pgwire")],
                "PgWire commit and rollback",
            )
            try:
                with psycopg.connect(
                    **{**pg_options, "password": "deliberately-wrong"}
                ):
                    pass
            except psycopg.OperationalError:
                pass
            else:
                raise AssertionError("PgWire accepted a wrong password")
            print(
                "PASS PgWire authentication, shared data, macros, commit/rollback, wrong-password rejection",
                flush=True,
            )
            # Exercise explicit Basic-auth handshake as well as ADBC's username/password flow.
            with flight.FlightClient(f"grpc://{flight_address}") as client:
                token = client.authenticate_basic_token(
                    username, password, options=flight.FlightCallOptions(timeout=10)
                )
                if not token[1]:
                    raise AssertionError(
                        "Flight handshake did not return a bearer token"
                    )
            with (
                flightsql.connect(
                    uri=f"grpc://{flight_address}",
                    db_kwargs={"username": username, "password": password},
                    autocommit=True,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("select * from alpha_shared order by id")
                table = cursor.fetch_arrow_table()
                require_equal(
                    table.to_pylist(),
                    [{"id": 1, "label": "host"}, {"id": 2, "label": "pgwire"}],
                    "ADBC Flight SQL shared data",
                )
                require_equal(table.schema.names, ["id", "label"], "ADBC result schema")
                cursor.execute(
                    "select initcap('hELLO wORLD') as title, array_remove([1,2,1],1) as removed, array_replace([1,2,1],1,3) as replaced"
                )
                require_equal(
                    cursor.fetch_arrow_table().to_pylist(),
                    [{"title": "Hello World", "removed": [2], "replaced": [3, 2, 3]}],
                    "Flight compatibility macros",
                )
                cursor.execute(
                    "select column_name from information_schema.columns where table_name='alpha_shared' order by ordinal_position"
                )
                require_equal(
                    cursor.fetchall(), [("id",), ("label",)], "Flight schema discovery"
                )
            print(
                "PASS Flight Basic handshake, ADBC authenticated query, shared data, macros, schema discovery",
                flush=True,
            )
        finally:
            try:
                for protocol, address in reversed(servers):
                    host.query(
                        f"select * from duckflight_stop({sql_literal(protocol)}, {sql_literal(address)})"
                    )
                require_equal(
                    host.query("select * from duckflight_servers()") if servers else [],
                    [],
                    "all listeners stopped",
                )
            finally:
                host.close()


class AlphaDuckflightTests(unittest.TestCase):
    def test_real_alpha_clients(self):
        if not os.environ.get("DUCKDB_ALPHA_LIBRARY"):
            self.skipTest(
                "set DUCKDB_ALPHA_LIBRARY and DUCKFLIGHT_ALPHA_EXTENSION or DUCKFLIGHT_ALPHA_REPOSITORY"
            )
        repository = os.environ.get("DUCKFLIGHT_ALPHA_REPOSITORY")
        command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
        if repository:
            command.extend(["--repository", repository])
        elif os.environ.get("DUCKFLIGHT_ALPHA_ALLOW_UNSIGNED") == "1":
            command.append("--allow-unsigned")
        timeout = int(os.environ.get("DUCKFLIGHT_ALPHA_TEST_TIMEOUT", "120"))
        if timeout <= 0:
            self.fail("DUCKFLIGHT_ALPHA_TEST_TIMEOUT must be positive")
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as error:
            self.fail(
                f"Alpha client test exceeded {timeout} seconds.\n"
                f"stdout: {error.stdout!r}\nstderr: {error.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout, end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", help="signed repository URL or local prefix")
    parser.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="explicitly allow an unsigned local artifact",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repository and args.allow_unsigned:
        parser.error("repository tests always require signature validation")
    if args.worker:
        run_clients(args.repository, args.allow_unsigned)
    else:
        if args.repository:
            os.environ["DUCKFLIGHT_ALPHA_REPOSITORY"] = args.repository
        if args.allow_unsigned:
            os.environ["DUCKFLIGHT_ALPHA_ALLOW_UNSIGNED"] = "1"
        unittest.main(argv=[sys.argv[0]])


if __name__ == "__main__":
    main()

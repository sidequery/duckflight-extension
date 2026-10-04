#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.6", "pyarrow", "protobuf", "psycopg[binary]>=3.2,<4"]
# ///
"""Exercise persistent Flight sessions against a loaded public extension bundle."""

from __future__ import annotations

import argparse
import hashlib
import secrets
import tempfile
from pathlib import Path
from typing import Self

import duckdb
import psycopg
from google.protobuf.any_pb2 import Any as ProtobufAny
from pyarrow import flight

USERNAME = "host_session_test"
PASSWORD = secrets.token_urlsafe(24)
SESSION_COUNT = 128


def assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def command(sql: str) -> flight.FlightDescriptor:
    # CommandStatementQuery.query is a length-delimited field numbered 1.
    query = sql.encode()
    length = len(query)
    encoded_length = bytearray()
    while length >= 128:
        encoded_length.append((length & 127) | 128)
        length >>= 7
    encoded_length.append(length)
    message = ProtobufAny(
        type_url="type.googleapis.com/arrow.flight.protocol.sql.CommandStatementQuery",
        value=b"\x0a" + bytes(encoded_length) + query,
    )
    return flight.FlightDescriptor.for_command(message.SerializeToString())


class Session:
    def __init__(self, address: str) -> None:
        self.client = flight.FlightClient(f"grpc://{address}")
        self.closed = False
        token = self.client.authenticate_basic_token(
            USERNAME, PASSWORD, flight.FlightCallOptions(timeout=10)
        )
        self.options = flight.FlightCallOptions(headers=[token], timeout=10)

    def query(self, sql: str) -> list[dict]:
        info = self.client.get_flight_info(command(sql), self.options)
        rows = []
        for endpoint in info.endpoints:
            rows.extend(
                self.client.do_get(endpoint.ticket, self.options).read_all().to_pylist()
            )
        return rows

    def close_session(self) -> None:
        if not self.closed:
            results = list(
                self.client.do_action(flight.Action("CloseSession", b""), self.options)
            )
            assert_equal(len(results), 1, "CloseSession result count")
            assert_equal(results[0].body.to_pybytes(), b"\x08\x01", "session closed")
            self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            self.close_session()
        finally:
            self.client.close()


def write_config(path: Path) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", PASSWORD.encode(), salt, 10_000, dklen=32
    ).hex()
    path.write_text(
        f'''[users.{USERNAME}]
password_hash = "{digest}"
salt = {list(salt)}
iterations = 10000
'''
    )
    path.chmod(0o600)


def assert_fresh(session: Session) -> None:
    assert_equal(
        session.query(
            "select count(*) as n from duckdb_tables() "
            "where temporary and table_name = 'session_private'"
        ),
        [{"n": 0}],
        "fresh session temporary catalog",
    )
    assert_equal(
        session.query(
            "select count(*) as n from duckdb_functions() "
            "where function_name = 'session_private_macro'"
        ),
        [{"n": 0}],
        "fresh session macro catalog",
    )
    assert_equal(
        session.query(
            "select getvariable('session_private_value') is null as cleared, "
            "current_setting('TimeZone') as timezone"
        ),
        [{"cleared": True, "timezone": "UTC"}],
        "fresh session variables and settings",
    )
    try:
        session.query("execute session_private_statement")
    except flight.FlightError as error:
        message = str(error).lower()
        if "prepared statement" not in message or "does not exist" not in message:
            raise
    else:
        raise AssertionError("fresh session inherited a SQL prepared statement")


def check_repeated_sessions(host: duckdb.DuckDBPyConnection, address: str) -> None:
    for index in range(SESSION_COUNT):
        with Session(address) as session:
            assert_fresh(session)
            assert_equal(
                session.query(
                    "select sum(value)::bigint as total from host_session_values"
                ),
                [{"total": index + 1}],
                "session sees host table and later host changes",
            )
            session.query("create temp table session_private as select 42 as value")
            session.query("create temp macro session_private_macro() as 7")
            session.query("set variable session_private_value = 13")
            session.query("set TimeZone='Pacific/Honolulu'")
            session.query("prepare session_private_statement as select 19 as value")
            assert_equal(
                session.query(
                    "select value, session_private_macro() as macro_value, "
                    "getvariable('session_private_value') as variable_value, "
                    "current_setting('TimeZone') as timezone from session_private"
                ),
                [
                    {
                        "value": 42,
                        "macro_value": 7,
                        "variable_value": 13,
                        "timezone": "Pacific/Honolulu",
                    }
                ],
                "state persists across session RPCs",
            )
            assert_equal(
                session.query("execute session_private_statement"),
                [{"value": 19}],
                "SQL prepared statement persists",
            )
            session.query("begin")
            session.query("update host_session_rollback set value = -1 where id = 1")
            session.close_session()
            if index == 0:
                try:
                    session.query("select 42")
                except flight.FlightUnauthenticatedError:
                    pass
                else:
                    raise AssertionError("closed session credentials remain usable")

        # Reading alone cannot prove rollback: uncommitted changes are invisible.
        # Updating the same row also proves the closed transaction released its
        # write conflict instead of retaining a hidden live connection forever.
        host.execute("update host_session_rollback set value = ? where id = 1", [index])
        assert_equal(
            host.execute(
                "select value from host_session_rollback where id = 1"
            ).fetchone(),
            (index,),
            "CloseSession rolls back and releases the write conflict",
        )
        host.execute("insert into host_session_values values (1)")
    print(
        f"PASS {SESSION_COUNT} fresh sessions, state persistence/isolation, and close rollback",
        flush=True,
    )


def check_simultaneous_sessions(address: str) -> None:
    with Session(address) as first, Session(address) as second:
        first.query("create temp table session_private as select 101 as value")
        first.query("set TimeZone='Pacific/Honolulu'")
        assert_fresh(second)
        second.query("create temp table session_private as select 202 as value")
        assert_equal(
            first.query("select value from session_private"),
            [{"value": 101}],
            "first session",
        )
        assert_equal(
            second.query("select value from session_private"),
            [{"value": 202}],
            "second session",
        )
    print("PASS simultaneous sessions have independent state", flush=True)


def check_independent_listener_lifetimes(host: duckdb.DuckDBPyConnection, config: Path) -> None:
    addresses: dict[str, str] = {}

    def start(protocol: str) -> str:
        function = "duckflight_flight_serve" if protocol == "flight" else "duckflight_pg_serve"
        address = host.execute(
            f"select address from {function}('127.0.0.1:0', ?)", [str(config)]
        ).fetchone()[0]
        addresses[protocol] = address
        return address

    def stop(protocol: str) -> None:
        host.execute("select * from duckflight_stop(?, ?)", [protocol, addresses[protocol]]).fetchall()
        del addresses[protocol]

    def connect_pg() -> psycopg.Connection:
        hostname, port = addresses["pgwire"].rsplit(":", 1)
        return psycopg.connect(host=hostname, port=int(port), user=USERNAME,
                               password=PASSWORD, dbname="duckflight", autocommit=True,
                               connect_timeout=10)

    try:
        start("flight")
        start("pgwire")
        with connect_pg() as pg:
            pg.execute("create temp table pg_survivor as select 31 as value")
            stop("flight")
            assert_equal(pg.execute("select value from pg_survivor").fetchone(),
                         (31,), "PgWire session survives Flight shutdown")
            start("flight")
            with Session(addresses["flight"]) as session:
                assert_equal(session.query("select 42 as value"), [{"value": 42}],
                             "restarted Flight accepts work")
        with Session(addresses["flight"]) as session:
            session.query("create temp table flight_survivor as select 37 as value")
            stop("pgwire")
            assert_equal(session.query("select value from flight_survivor"), [{"value": 37}],
                         "Flight session survives PgWire shutdown")
            start("pgwire")
            with connect_pg() as pg:
                assert_equal(pg.execute("select 43").fetchone(), (43,),
                             "restarted PgWire accepts work")
    finally:
        for protocol in list(addresses):
            stop(protocol)
    print("PASS independent Flight/PgWire stop and restart", flush=True)


def run(extension: Path) -> None:
    if not extension.is_file():
        raise FileNotFoundError(extension)
    address = None
    with tempfile.TemporaryDirectory(prefix="duckflight-host-sessions-") as directory:
        config = Path(directory) / "auth.toml"
        write_config(config)
        with duckdb.connect(config={"allow_unsigned_extensions": "true"}) as host:
            try:
                host.execute(
                    "create table host_session_values as select 1::bigint as value"
                )
                host.execute(
                    "create table host_session_rollback(id integer primary key, value integer)"
                )
                host.execute("insert into host_session_rollback values (1, 0)")
                host.execute(
                    "load '" + str(extension.resolve()).replace("'", "''") + "'"
                )
                assert_equal(
                    host.execute(
                        "select loaded, abi_version from duckflight_core_status()"
                    ).fetchone(),
                    (True, 1),
                    "bundled ABI v1 core",
                )
                address = host.execute(
                    "select address from duckflight_flight_serve('127.0.0.1:0', ?)",
                    [str(config)],
                ).fetchone()[0]
                check_repeated_sessions(host, address)
                check_simultaneous_sessions(address)
                host.execute(
                    "select * from duckflight_stop('flight', ?)", [address]
                ).fetchall()
                address = None
                address = host.execute(
                    "select address from duckflight_flight_serve('127.0.0.1:0', ?)",
                    [str(config)],
                ).fetchone()[0]
                with Session(address) as session:
                    assert_fresh(session)
                    assert_equal(
                        session.query(
                            "select sum(value)::bigint as total from host_session_values"
                        ),
                        [{"total": SESSION_COUNT + 1}],
                        "restart preserves host database",
                    )
                print("PASS server restart retains access to host database", flush=True)
                check_independent_listener_lifetimes(host, config)
            finally:
                if address is not None:
                    host.execute(
                        "select * from duckflight_stop('flight', ?)", [address]
                    ).fetchall()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extension", type=Path, help="bundled .duckdb_extension path")
    run(parser.parse_args().extension)

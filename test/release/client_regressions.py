#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "adbc-driver-gizmosql==2.0.12",
#   "duckdb==1.5.5",
#   "psycopg[binary]>=3.2,<4",
#   "psycopg2-binary>=2.9,<3",
#   "pyarrow",
#   "SQLAlchemy>=2,<3",
# ]
# ///
"""Run release-critical client regressions against a bundled DuckFlight extension."""

from __future__ import annotations

import argparse
import hashlib
import secrets
import tempfile
import time
from pathlib import Path

import adbc_driver_gizmosql.dbapi as gizmosql
import duckdb
import psycopg
import pyarrow as pa
from pyarrow import flight
from sqlalchemy import Numeric, create_engine, inspect
from sqlalchemy.engine import URL

USERNAME = "release_regression"
PASSWORD = secrets.token_urlsafe(24)
SCHEMA = "release_regression"

TPC_H_Q20 = """
select s_name, s_address
from supplier, nation
where s_suppkey in (
    select ps_suppkey
    from partsupp
    where ps_partkey in (
        select p_partkey
        from part
        where p_name like 'forest%'
    )
      and ps_availqty > (
          select 0.5 * sum(l_quantity)
          from lineitem
          where l_partkey = ps_partkey
            and l_suppkey = ps_suppkey
            and l_shipdate >= date '1994-01-01'
            and l_shipdate < date '1994-01-01' + interval '1 year'
      )
)
  and s_nationkey = n_nationkey
  and n_name = 'CANADA'
order by s_name
"""


def assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_config(path: Path) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", PASSWORD.encode(), salt, 10_000, dklen=32
    ).hex()
    path.write_text(
        f"""[users.{USERNAME}]
password_hash = "{digest}"
salt = {list(salt)}
iterations = 10000
"""
    )
    path.chmod(0o600)


def pg_connect(address: str) -> psycopg.Connection:
    host, raw_port = address.rsplit(":", 1)
    deadline = time.monotonic() + 10
    while True:
        try:
            return psycopg.connect(
                host=host,
                port=int(raw_port),
                user=USERNAME,
                password=PASSWORD,
                dbname="duckflight",
                connect_timeout=2,
                autocommit=True,
            )
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def sqlalchemy_url(address: str) -> URL:
    host, raw_port = address.rsplit(":", 1)
    return URL.create(
        "postgresql+psycopg2",
        username=USERNAME,
        password=PASSWORD,
        host=host,
        port=int(raw_port),
        database="duckflight",
    )


def check_sqlalchemy_reflection(address: str) -> None:
    with pg_connect(address) as connection:
        connection.execute(f"drop schema if exists {SCHEMA} cascade")
        connection.execute(f"create schema {SCHEMA}")
        connection.execute(f"create table {SCHEMA}.parent_a (id integer primary key)")
        connection.execute(f"create table {SCHEMA}.parent_b (id integer primary key)")
        connection.execute(
            f"""create table {SCHEMA}.child (
                id integer primary key,
                amount numeric(12, 2) not null,
                parent_a_id integer references {SCHEMA}.parent_a(id),
                parent_b_id integer references {SCHEMA}.parent_b(id)
            )"""
        )

    engine = create_engine(sqlalchemy_url(address), connect_args={"connect_timeout": 5})
    try:
        inspector = inspect(engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("child", schema=SCHEMA)
        }
        amount = columns["amount"]["type"]
        if not isinstance(amount, Numeric):
            raise TypeError(f"numeric reflection returned {amount!r}")
        assert_equal(amount.precision, 12, "numeric precision")
        assert_equal(amount.scale, 2, "numeric scale")

        foreign_keys = {
            (
                tuple(item["constrained_columns"]),
                item["referred_schema"],
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys("child", schema=SCHEMA)
        }
        assert_equal(
            foreign_keys,
            {
                (("parent_a_id",), SCHEMA, "parent_a", ("id",)),
                (("parent_b_id",), SCHEMA, "parent_b", ("id",)),
            },
            "multiple foreign-key reflection",
        )
    finally:
        engine.dispose()


def check_unknown_placeholders(address: str) -> None:
    expected = {
        "pg_description": ("objoid", "classoid", "objsubid", "description"),
        "pg_shdescription": ("objoid", "classoid", "description"),
    }
    with pg_connect(address) as connection:
        for relation, names in expected.items():
            cursor = connection.execute(
                f"select * from pg_catalog.{relation} limit 0", prepare=True
            )
            assert_equal(
                tuple(column.name for column in cursor.description),
                names,
                f"{relation} wire column names",
            )
            assert_equal(
                tuple(column.type_code for column in cursor.description),
                (25,) * len(names),
                f"{relation} wire text OIDs",
            )

        rows = connection.execute(
            """with expected(relation_name, column_name) as (
                values ('pg_description', 'objoid'),
                       ('pg_description', 'classoid'),
                       ('pg_description', 'objsubid'),
                       ('pg_description', 'description'),
                       ('pg_shdescription', 'objoid'),
                       ('pg_shdescription', 'classoid'),
                       ('pg_shdescription', 'description')
            )
            select e.relation_name, e.column_name, a.atttypid, dc.data_type
            from expected e
            join pg_catalog.pg_class c on c.relname = e.relation_name
            join pg_catalog.pg_attribute a
              on a.attrelid = c.oid and a.attname = e.column_name
            join duckdb_columns() dc
              on dc.schema_name = 'duckflight_pg_catalog'
             and dc.table_name = e.relation_name
             and dc.column_name = e.column_name
            where c.relnamespace = 11
            order by e.relation_name, e.column_name"""
        ).fetchall()
        assert_equal(len(rows), 7, "UNKNOWN placeholder catalog row count")
        for relation, column, type_oid, physical_type in rows:
            assert_equal(type_oid, 25, f"{relation}.{column} pg_attribute type")
            assert_equal(physical_type, "VARCHAR", f"{relation}.{column} physical type")


def check_failed_transaction_rollback(address: str) -> None:
    with pg_connect(address) as connection:
        connection.execute("create temp table release_rollback_rows(id integer)")
        connection.execute("begin")
        connection.execute("insert into release_rollback_rows values (1)")
        try:
            connection.execute("select error('release rollback regression')")
        except psycopg.Error:
            pass
        else:
            raise AssertionError("transaction fixture did not fail")

        try:
            connection.execute("select 1")
        except psycopg.Error as error:
            assert_equal(error.sqlstate, "25P02", "failed transaction SQLSTATE")
        else:
            raise AssertionError("failed transaction accepted a query before rollback")

        connection.execute("rollback")
        count = connection.execute(
            "select count(*) from release_rollback_rows"
        ).fetchone()[0]
        assert_equal(count, 0, "failed transaction rollback")


def check_flight_regressions(address: str) -> None:
    with (
        gizmosql.connect(
            uri=f"grpc://{address}",
            db_kwargs={"username": USERNAME, "password": PASSWORD},
            autocommit=True,
        ) as connection,
        connection.cursor() as cursor,
    ):
        # GizmoSQL ADBC prepares statements before execution. This catches
        # regressions where preparing SET tries to append LIMIT 0.
        cursor.execute("set threads=2")
        cursor.execute("select current_setting('threads')::integer as threads")
        assert_equal(cursor.fetchall(), [(2,)], "prepared SET")

        cursor.execute(TPC_H_Q20)
        advertised_names = tuple(column[0] for column in cursor.description)
        table = cursor.fetch_arrow_table()
        if not isinstance(table, pa.Table):
            raise TypeError(f"Q20 returned {type(table)!r} instead of pyarrow.Table")
        assert_equal(
            advertised_names,
            tuple(table.schema.names),
            "Q20 advertised/stream names",
        )
        assert_equal(
            table.to_pylist(),
            [{"s_name": "Supplier One", "s_address": "Address One"}],
            "Q20 result",
        )
        if not all(field.nullable for field in table.schema):
            raise AssertionError(f"Q20 stream schema is not nullable: {table.schema}")


def protobuf_strings(fields: dict[int, bytes]) -> bytes:
    # These Flight SQL command fixtures contain only short length-delimited
    # fields. Encode their standard protobuf wire format without generated stubs.
    encoded = bytearray()
    for tag, value in fields.items():
        if not 1 <= tag < 16 or len(value) >= 128:
            raise ValueError("command fixture exceeds the short-field encoding")
        encoded.extend(bytes([(tag << 3) | 2, len(value)]))
        encoded.extend(value)
    return bytes(encoded)


def check_flight_key_metadata(address: str) -> None:
    with flight.FlightClient(f"grpc://{address}") as client:
        token = client.authenticate_basic_token(USERNAME, PASSWORD)
        options = flight.FlightCallOptions(headers=[token], timeout=30)
        for empty in (False, True):
            schema = SCHEMA.encode()
            parent = b"missing_release_table" if empty else b"parent_a"
            child = b"missing_release_table" if empty else b"child"
            cases = [
                ("PrimaryKeys", {2: schema, 3: parent}, 1),
                ("ImportedKeys", {2: schema, 3: child}, 2),
                ("ExportedKeys", {2: schema, 3: parent}, 1),
                (
                    "CrossReference",
                    {2: schema, 3: b"parent_a", 5: schema, 6: child},
                    1,
                ),
            ]
            for name, fields, populated_rows in cases:
                command = protobuf_strings(fields)
                type_url = (
                    "type.googleapis.com/arrow.flight.protocol.sql.CommandGet" + name
                ).encode()
                descriptor = flight.FlightDescriptor.for_command(
                    protobuf_strings({1: type_url, 2: command})
                )
                info = client.get_flight_info(descriptor, options)
                advertised = client.get_schema(descriptor, options).schema
                if not info.schema.equals(advertised, check_metadata=True):
                    raise AssertionError(f"{name}: GetFlightInfo/GetSchema mismatch")
                count = 0
                for endpoint in info.endpoints:
                    reader = client.do_get(endpoint.ticket, options)
                    if not advertised.equals(reader.schema, check_metadata=True):
                        raise AssertionError(
                            f"{name}: advertised/stream schema mismatch"
                        )
                    count += reader.read_all().num_rows
                assert_equal(count, 0 if empty else populated_rows, f"{name} row count")


def prepare_flight_fixture(host: duckdb.DuckDBPyConnection) -> None:
    host.execute(
        """create table supplier (
               s_suppkey integer primary key,
               s_name varchar not null,
               s_address varchar not null,
               s_nationkey integer
           );
           create table nation (n_nationkey integer primary key, n_name varchar);
           create table part (p_partkey integer primary key, p_name varchar);
           create table partsupp (
               ps_partkey integer,
               ps_suppkey integer,
               ps_availqty integer
           );
           create table lineitem (
               l_partkey integer,
               l_suppkey integer,
               l_quantity decimal(15, 2),
               l_shipdate date
           );
           insert into supplier values (1, 'Supplier One', 'Address One', 1);
           insert into nation values (1, 'CANADA');
           insert into part values (1, 'forest green');
           insert into partsupp values (1, 1, 100);
           insert into lineitem values (1, 1, 10, date '1994-02-01');"""
    )


def run(extension: Path) -> None:
    if not extension.is_file():
        raise FileNotFoundError(f"extension does not exist: {extension}")

    pg_address: str | None = None
    flight_address: str | None = None
    host = duckdb.connect(config={"allow_unsigned_extensions": "true"})
    with tempfile.TemporaryDirectory(prefix="duckflight-release-") as raw_tempdir:
        config_path = Path(raw_tempdir) / "auth.toml"
        write_config(config_path)
        try:
            host.execute(f"load {sql_literal(str(extension.resolve()))}")
            status = host.execute(
                "select loaded, abi_version from duckflight_core_status()"
            ).fetchone()
            assert_equal(status, (True, 1), "bundled core status")
            prepare_flight_fixture(host)

            pg_address = host.execute(
                "select address from duckflight_pg_serve('127.0.0.1:0', ?)",
                [str(config_path)],
            ).fetchone()[0]
            flight_address = host.execute(
                "select address from duckflight_flight_serve('127.0.0.1:0', ?)",
                [str(config_path)],
            ).fetchone()[0]

            check_sqlalchemy_reflection(pg_address)
            print("PASS SQLAlchemy numeric and multiple-FK reflection", flush=True)
            check_unknown_placeholders(pg_address)
            print("PASS UNKNOWN placeholder catalog and wire types", flush=True)
            check_failed_transaction_rollback(pg_address)
            print("PASS failed transaction rollback", flush=True)
            check_flight_key_metadata(flight_address)
            print(
                "PASS all four Flight key-metadata schemas, populated and empty",
                flush=True,
            )
            check_flight_regressions(flight_address)
            print("PASS GizmoSQL ADBC prepared SET and TPC-H Q20 schema", flush=True)
        finally:
            try:
                if flight_address is not None:
                    host.execute(
                        "select * from duckflight_stop('flight', ?)", [flight_address]
                    ).fetchall()
            finally:
                try:
                    if pg_address is not None:
                        host.execute(
                            "select * from duckflight_stop('pgwire', ?)", [pg_address]
                        ).fetchall()
                finally:
                    host.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run public-bundle DuckFlight client regressions"
    )
    parser.add_argument("extension", type=Path, help="bundled .duckdb_extension path")
    args = parser.parse_args()
    run(args.extension)


if __name__ == "__main__":
    main()

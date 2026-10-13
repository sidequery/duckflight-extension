"""In-cluster lake writer and real PostgreSQL/Flight SQL assertions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import boto3
import duckdb
import psycopg
import pyarrow as pa
from adbc_driver_flightsql import DatabaseOptions
from adbc_driver_flightsql.dbapi import connect as adbc_connect
from botocore.config import Config
from docker_smoke import flight_query
from pyarrow import flight
from pyiceberg.catalog import load_catalog

CATALOG = os.environ["TEST_CATALOG"]
ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
SECRET_KEY = os.environ["S3_SECRET_KEY"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
S3_ENDPOINT = "http://object-store:8333"
ROWS = [{"id": 1, "amount": 10}, {"id": 2, "amount": 20}, {"id": 3, "amount": 30}]
QUERY = "select id, amount from lake.analytics.events order by id"


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def lake_connection():
    connection = duckdb.connect()
    connection.execute("install httpfs; load httpfs;")
    connection.execute(
        f"""create secret storage (
        type s3, key_id '{ACCESS_KEY}', secret '{SECRET_KEY}',
        endpoint 'object-store:8333', url_style 'path', use_ssl false,
        scope 's3://warehouse/ducklake/'
        );"""
    )
    connection.execute(
        "install postgres; load postgres; install ducklake; load ducklake;"
    )
    connection.execute(
        "attach 'ducklake:postgres:host=postgres dbname=ducklake_catalog "
        f"user=postgres password={POSTGRES_PASSWORD}' as lake "
        "(data_path 's3://warehouse/ducklake/');"
    )
    # Force small test rows out of PostgreSQL's inlining optimization: the test
    # must actually exercise S3 Parquet reads, not just catalog metadata reads.
    connection.execute("call lake.set_option('data_inlining_row_limit', 0)")
    return connection


def iceberg_catalog():
    return load_catalog(
        "e2e",
        type="rest",
        uri="http://iceberg:8181",
        warehouse="s3://warehouse/iceberg/",
        **{
            "s3.endpoint": S3_ENDPOINT,
            "s3.access-key-id": ACCESS_KEY,
            "s3.secret-access-key": SECRET_KEY,
            "s3.region": "us-east-1",
        },
    )


def seed(append: bool) -> None:
    storage = s3_client()
    if not append:
        storage.create_bucket(Bucket="warehouse")
    records = [{"id": 4, "amount": 40}] if append else ROWS
    if CATALOG == "ducklake":
        with lake_connection() as connection:
            if not append:
                connection.execute("create schema lake.analytics")
                connection.execute(
                    "create table lake.analytics.events(id bigint, amount bigint)"
                )
            connection.executemany(
                "insert into lake.analytics.events values (?, ?)",
                [(row["id"], row["amount"]) for row in records],
            )
    else:
        catalog = iceberg_catalog()
        if not append:
            catalog.create_namespace("analytics")
            table = catalog.create_table(
                "analytics.events",
                schema=pa.schema([("id", pa.int64()), ("amount", pa.int64())]),
                location="s3://warehouse/iceberg/events",
                properties={"format-version": "2"},
            )
        else:
            table = catalog.load_table("analytics.events")
        table.append(pa.Table.from_pylist(records))
    objects = storage.list_objects_v2(Bucket="warehouse", Prefix=f"{CATALOG}/")
    assert any(item["Key"].endswith(".parquet") for item in objects.get("Contents", []))
    print(f"PASS {CATALOG} external writer committed rows and stored Parquet in S3")


def verify(updated: bool) -> None:
    host = os.environ["TEST_HOST"]
    address = os.environ.get("TEST_POD_IP", "")
    tls_cert = Path("/run/test/server.crt")
    expected = ROWS + ([{"id": 4, "amount": 40}] if updated else [])
    parameters = {
        "host": host,
        "port": 5433,
        "user": "e2e",
        "password": os.environ["CLIENT_PASSWORD"],
        "dbname": "lake",
        "sslmode": "verify-full",
        "sslrootcert": str(tls_cert),
        "connect_timeout": 10,
        "autocommit": True,
    }
    if address:
        parameters["hostaddr"] = address
    with psycopg.connect(**parameters) as connection:
        assert connection.execute(
            "select duckflight_visible_database()"
        ).fetchone() == ("lake",)
        assert connection.execute(QUERY).fetchall() == [
            (row["id"], row["amount"]) for row in expected
        ]
        assert connection.execute(
            "select sum(amount) from lake.analytics.events"
        ).fetchone() == (sum(row["amount"] for row in expected),)
        assert connection.execute(
            "select current_setting('enable_external_access')::boolean, "
            "current_setting('lock_configuration')::boolean"
        ).fetchone() == (False, True)
        for sql in (
            "select content from read_text('/run/secrets/duckflight.toml')",
            "select content from read_text('/run/bootstrap/init.sql')",
            "set enable_external_access=true",
            "insert into lake.analytics.events values (99, 99)",
        ):
            try:
                connection.execute(sql)
            except psycopg.Error:
                pass
            else:
                raise AssertionError(f"PostgreSQL unexpectedly accepted {sql}")
    parameters["password"] = "incorrect-password"
    try:
        with psycopg.connect(**parameters):
            pass
    except psycopg.OperationalError:
        pass
    else:
        raise AssertionError("PostgreSQL accepted incorrect credentials")
    with flight.FlightClient(
        f"grpc+tls://{address or host}:31337",
        tls_root_certs=tls_cert.read_bytes(),
        override_hostname=host,
    ) as client:
        options = flight.FlightCallOptions(
            headers=[
                (b"authorization", f"Bearer {os.environ['CLIENT_TOKEN']}".encode())
            ],
            timeout=30,
        )
        assert flight_query(client, options, QUERY) == expected
        assert flight_query(
            client,
            options,
            "select sum(amount)::bigint as total from lake.analytics.events",
        ) == [{"total": sum(row["amount"] for row in expected)}]
        for sql in (
            "select content from read_text('/run/secrets/duckflight.toml')",
            "select content from read_text('/run/bootstrap/init.sql')",
        ):
            try:
                flight_query(client, options, sql)
            except (flight.FlightError, pa.ArrowInvalid):
                pass
            else:
                raise AssertionError(f"Flight unexpectedly accepted {sql}")
        bad_options = flight.FlightCallOptions(
            headers=[(b"authorization", b"Bearer incorrect-token")], timeout=10
        )
        try:
            flight_query(client, bad_options, QUERY)
        except flight.FlightUnauthenticatedError:
            pass
        else:
            raise AssertionError("Flight accepted incorrect credentials")
    # Exercise the standard driver's connection, statement, endpoint routing,
    # and Arrow result path, in addition to the low-level protocol assertions.
    with (
        adbc_connect(
            f"grpc+tls://{address or host}:31337",
            db_kwargs={
                DatabaseOptions.AUTHORIZATION_HEADER.value: f"Bearer {os.environ['CLIENT_TOKEN']}",
                DatabaseOptions.TLS_ROOT_CERTS.value: tls_cert.read_text(),
                DatabaseOptions.TLS_OVERRIDE_HOSTNAME.value: host,
            },
            autocommit=True,
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(QUERY)
        assert cursor.fetch_arrow_table().to_pylist() == expected
    print(
        f"PASS {CATALOG} TLS PgWire + Flight + ADBC rows/aggregate, locked policy, "
        f"secret isolation, readonly catalog and authentication at {address or host}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["seed", "append", "verify", "verify-updated", "idle"]
    )
    mode = parser.parse_args().mode
    if mode == "idle":
        from idle_client import main as idle_cycle

        idle_cycle()
    elif mode in ("seed", "append"):
        seed(mode == "append")
    else:
        verify(mode == "verify-updated")

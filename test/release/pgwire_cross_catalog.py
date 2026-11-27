#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2,<4", "pyarrow>=18,<24"]
# ///
"""Verify the opt-in PgWire cross-catalog read setting in the actual bundled extension."""

from __future__ import annotations

import argparse
import hashlib
import secrets
import tempfile
from pathlib import Path

import psycopg
from http_quack_smoke import test_host

USER = "cross_catalog_test"
DATABASE_DOES_NOT_EXIST = "3D000"
CROSS_CATALOG_JOIN = (
    "select v.id, t.id from lake_atm_views.events.revenue_view v "
    "join analytics.revenue t on true"
)


def write_config(path: Path, password: str, cross_catalog_reads: bool) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, 10_000, dklen=32
    ).hex()
    setting = "[pgwire]\ncross_catalog_reads = true\n\n" if cross_catalog_reads else ""
    path.write_text(
        f"{setting}[users.{USER}]\n"
        f'password_hash = "{digest}"\nsalt = {list(salt)}\niterations = 10000\n'
    )
    path.chmod(0o600)


def connect(address: str, password: str) -> psycopg.Connection:
    server, port = address.rsplit(":", 1)
    return psycopg.connect(
        host=server,
        port=int(port),
        user=USER,
        password=password,
        dbname="lake_atm",
        autocommit=True,
        connect_timeout=5,
    )


def assert_rejected(connection: psycopg.Connection, query: str) -> None:
    try:
        connection.execute(query).fetchall()
    except psycopg.Error as error:
        if error.sqlstate != DATABASE_DOES_NOT_EXIST:
            raise AssertionError(
                f"expected SQLSTATE {DATABASE_DOES_NOT_EXIST}, got {error.sqlstate}: {query}"
            ) from error
        return
    raise AssertionError(f"cross-catalog statement was not rejected: {query}")


def check_enabled(address: str, password: str) -> None:
    with connect(address, password) as connection:
        if connection.execute(CROSS_CATALOG_JOIN).fetchall() != [(42, 42)]:
            raise AssertionError("cross-catalog join returned the wrong rows")
        prepared = connection.execute(
            CROSS_CATALOG_JOIN + " where v.id = %s", [42], prepare=True
        ).fetchall()
        if prepared != [(42, 42)]:
            raise AssertionError("prepared cross-catalog join returned the wrong rows")
        # Two-part names still resolve in the connected database, not the host's.
        if connection.execute("select id from analytics.revenue").fetchall() != [(42,)]:
            raise AssertionError("two-part name left the connected database")
        assert_rejected(connection, "select id from not_attached.events.revenue_view")
        assert_rejected(
            connection,
            "create table analytics.copied as "
            "select id from lake_atm_views.events.revenue_view",
        )


def check_disabled(address: str, password: str) -> None:
    with connect(address, password) as connection:
        assert_rejected(connection, CROSS_CATALOG_JOIN)
        if connection.execute("select id from analytics.revenue").fetchall() != [(42,)]:
            raise AssertionError("default listener could not read its own database")


def run(cli: Path, extension: Path | None, repository: str | None) -> None:
    with tempfile.TemporaryDirectory(
        prefix="duckflight-pgwire-cross-catalog-"
    ) as directory:
        fixtures = Path(directory)
        password = secrets.token_urlsafe(24)
        enabled_config = fixtures / "enabled.toml"
        disabled_config = fixtures / "disabled.toml"
        write_config(enabled_config, password, cross_catalog_reads=True)
        write_config(disabled_config, password, cross_catalog_reads=False)
        with test_host(cli, fixtures, extension, repository) as host:
            if host.execute(
                "select loaded, abi_version::bigint from duckflight_core_status()"
            ).fetchone() != (True, 1):
                raise AssertionError("bundled core did not initialize")
            host.execute(
                "create schema analytics; "
                "create table analytics.revenue as select 7 as id; "
                "attach ':memory:' as lake_atm; "
                "create schema lake_atm.analytics; "
                "create table lake_atm.analytics.revenue as select 42 as id; "
                "attach ':memory:' as lake_atm_views; "
                "create schema lake_atm_views.events; "
                "create view lake_atm_views.events.revenue_view as "
                "select id from lake_atm.analytics.revenue"
            )
            for config, check in (
                (enabled_config, check_enabled),
                (disabled_config, check_disabled),
            ):
                address = host.execute(
                    "select address from duckflight_serve('pgwire', '127.0.0.1:0', config_file := ?)",
                    [str(config)],
                ).fetchone()[0]
                try:
                    check(address, password)
                finally:
                    host.execute(
                        "select * from duckflight_stop('pgwire', ?)", [address]
                    )
    print(
        "PASS cross-catalog reads: opt-in join, prepared parameters, and default rejection"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", type=Path)
    parser.add_argument("extension", type=Path, nargs="?")
    parser.add_argument("--repository", help="signed extension repository prefix")
    args = parser.parse_args()
    if (args.extension is None) == (args.repository is None):
        parser.error("provide either an extension path or --repository")
    run(args.cli, args.extension, args.repository)

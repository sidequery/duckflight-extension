#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2,<4", "pyarrow>=18,<24"]
# ///
"""Verify attached-database PgWire queries in the actual bundled extension."""

from __future__ import annotations

import argparse
import hashlib
import secrets
import tempfile
from pathlib import Path

import psycopg
from http_quack_smoke import test_host


def run(cli: Path, extension: Path | None, repository: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="duckflight-pgwire-attached-") as directory:
        fixtures = Path(directory)
        password = secrets.token_urlsafe(24)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, 10_000, dklen=32
        ).hex()
        config = fixtures / "auth.toml"
        config.write_text(
            "[users.attached_test]\n"
            f'password_hash = "{digest}"\nsalt = {list(salt)}\niterations = 10000\n'
        )
        config.chmod(0o600)
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
                "create table lake_atm.analytics.revenue as select 42 as id"
            )
            address = host.execute(
                "select address from duckflight_pg_serve('127.0.0.1:0', ?)",
                [str(config)],
            ).fetchone()[0]
            try:
                server, port = address.rsplit(":", 1)
                with psycopg.connect(
                    host=server,
                    port=int(port),
                    user="attached_test",
                    password=password,
                    dbname="lake_atm",
                    autocommit=True,
                    connect_timeout=5,
                ) as connection:
                    connection.execute("set statement_timeout = 10000")
                    connection.execute("set search_path = pg_catalog")
                    for query in (
                        'select id from "analytics"."revenue" limit 100',
                        "select analytics.revenue.id from analytics.revenue",
                        "with sample as (select id from analytics.revenue) select id from sample",
                    ):
                        if connection.execute(query).fetchall() != [(42,)]:
                            raise AssertionError(
                                f"query resolved the wrong database: {query}"
                            )
                    if connection.execute(
                        "select id from analytics.revenue where id = %s",
                        [42],
                        prepare=True,
                    ).fetchall() != [(42,)]:
                        raise AssertionError(
                            "prepared query resolved the wrong database"
                        )
                    columns = connection.execute(
                        "select column_name from information_schema.columns "
                        "where table_schema = 'analytics' and table_name = 'revenue' "
                        "order by ordinal_position"
                    ).fetchall()
                    if columns != [("id",)]:
                        raise AssertionError(f"column discovery failed: {columns}")
                    helpers = connection.execute(
                        "select array_upper(array[1, 2], 1), "
                        "pg_catalog.array_lower(array[1, 2], 1), "
                        "quote_nullable(null), quote_nullable('hi'), btrim(' x ') "
                        "where quote_ident('order') in (select '\"order\"')"
                    ).fetchall()
                    if helpers != [(2, 1, "NULL", "'hi'", "x")]:
                        raise AssertionError(f"metadata helpers failed: {helpers}")
                    if connection.execute("select 1").fetchone() != (1,):
                        raise AssertionError(
                            "connection unusable after metadata queries"
                        )
            finally:
                host.execute("select * from duckflight_stop('pgwire', ?)", [address])
    print(
        "PASS attached database: quoted reads, prepared parameters, metadata, and helpers"
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

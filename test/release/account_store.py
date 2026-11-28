#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2,<4", "pyarrow>=18,<24"]
# ///
"""Verify the account store setup functions in the actual bundled extension.

A host creates its first administrator with SQL and serves without a config file. The
administrator adds a user and a token over PostgreSQL, the token authenticates over HTTP, a
legacy config file's accounts are imported, and the config file functions refuse to add
accounts the store would ignore. Clients cannot call any of the setup functions.

This needs a core with the account store; the core in core-assets.lock may predate it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import secrets
import tempfile
from pathlib import Path

import psycopg
from http_quack_smoke import request, test_host
from quack_client import WRITE_SCOPES

REFUSAL = "only be called from the process that owns the database"
LEGACY_PASSWORD = "legacy horse battery staple"


def connect(address: str, user: str, password: str) -> psycopg.Connection:
    server, port = address.rsplit(":", 1)
    return psycopg.connect(
        host=server,
        port=int(port),
        user=user,
        password=password,
        dbname="memory",
        autocommit=True,
        connect_timeout=5,
    )


def http(address: str, token: str, sql: str) -> tuple[int, bytes]:
    return request(
        f"http://{address}/streaming-query",
        {"query": sql, "useCache": False},
        token,
        None,
    )


def write_legacy_config(path: Path, token: str) -> None:
    """A config file from before the account store: a password user and a bearer token."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", LEGACY_PASSWORD.encode(), salt, 10_000, dklen=32
    ).hex()
    path.write_text(
        "shutdown_grace_secs = 1\n\n"
        f'[tokens.loader]\nsha256 = "{hashlib.sha256(token.encode()).hexdigest()}"\n'
        f'subject = "ci"\nscopes = {json.dumps(WRITE_SCOPES)}\n\n'
        f'[users.legacy]\npassword_hash = "{digest}"\n'
        f"salt = {list(salt)}\niterations = 10000\n"
    )
    path.chmod(0o600)


def assert_host_error(
    cli: Path, extension: Path, fixtures: Path, sql: str, expected: str
) -> None:
    """The test host stops at the first error, so each failing statement gets its own."""
    try:
        with test_host(cli, fixtures, extension, None) as host:
            host.execute(sql).fetchall()
    except RuntimeError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r}, got: {error}") from error
        return
    raise AssertionError(f"expected {expected!r} from: {sql}")


def run(cli: Path, extension: Path) -> None:
    cli, extension = cli.resolve(), extension.resolve()
    with tempfile.TemporaryDirectory() as directory:
        fixtures = Path(directory).resolve()
        legacy = fixtures / "legacy.toml"
        legacy_token = secrets.token_urlsafe(32)
        write_legacy_config(legacy, legacy_token)
        # An in-memory host keeps its account store under its working directory, so every
        # host started here shares this run's store and no other.
        store = fixtures / ".duckflight" / "store.duckdb"

        with contextlib.chdir(fixtures):
            with test_host(cli, fixtures, extension, None) as host:
                admin, admin_password = host.execute(
                    "select * from duckflight_init('alice')"
                ).fetchone()
                assert admin == "alice" and len(admin_password) >= 16, admin
                assert store.exists(), sorted(fixtures.rglob("*"))

                # No config file: the listeners authenticate against the store.
                addresses = {
                    protocol: host.execute(
                        "select address from duckflight_serve(?, '127.0.0.1:0')",
                        [protocol],
                    ).fetchone()[0]
                    for protocol in ("pgwire", "http")
                }
                host.execute("create table trips as select 1 as id")

                with connect(addresses["pgwire"], "alice", admin_password) as session:
                    user, bob_password = session.execute(
                        "create user bob role writer"
                    ).fetchone()
                    assert user == "bob" and bob_password, user
                    user, token_name, token = session.execute(
                        "create token nightly for user bob"
                    ).fetchone()
                    assert (user, token_name) == ("bob", "nightly") and token, user

                    # A client cannot use the host's setup functions, whatever its role.
                    for sql in (
                        "select * from duckflight_init('mallory')",
                        "select * from duckflight_user_add('mallory', role := 'admin')",
                        "select * from duckflight_token_add('alice', 'mine')",
                        "select * from duckflight_accounts()",
                        f"select * from duckflight_import('{legacy}')",
                    ):
                        try:
                            outcome = repr(session.execute(sql).fetchall())
                        except psycopg.Error as error:
                            outcome = str(error)
                        assert REFUSAL in outcome, (sql, outcome)

                with connect(addresses["pgwire"], "bob", bob_password) as session:
                    assert session.execute("select id from trips").fetchall() == [(1,)]
                status, body = http(
                    addresses["http"], token, "insert into trips values (2)"
                )
                assert status == 200, (status, body)
                status, _ = http(addresses["http"], token + "x", "select id from trips")
                assert status == 401, status
                # A PostgreSQL client logs in with a token as `<user>~<token name>`.
                with connect(addresses["pgwire"], "bob~nightly", token) as session:
                    assert session.execute("select current_user").fetchall() == [
                        ("bob",)
                    ]
                    session.execute("insert into trips values (3)")
                    session.execute("delete from trips where id = 3")
                for user, secret in (("bob", token), ("bob~nightly", token + "x")):
                    try:
                        connect(addresses["pgwire"], user, secret).close()
                        raise AssertionError(f"{user} logged in with a wrong secret")
                    except psycopg.OperationalError:
                        pass

                # The file's accounts are ignored until they are imported.
                row = host.execute(
                    "select store_path, store_exists, store_initialized, store_users::int, "
                    "ignored_users::int, ignored_tokens::int from duckflight_config_check(?)",
                    [str(legacy)],
                ).fetchone()
                assert Path(row[0]).resolve() == store.resolve(), row
                assert row[1:] == (True, True, 2, 1, 1), row
                status, _ = http(
                    addresses["http"], legacy_token, "select id from trips"
                )
                assert status == 401, status

                imported = host.execute(
                    "select kind, name, account, role from duckflight_import(?) order by kind",
                    [str(legacy)],
                ).fetchall()
                # A token's subject becomes a user without a password that owns the token.
                assert imported == [
                    ("token", "loader", "ci", "writer"),
                    ("user", "ci", "ci", "writer"),
                    ("user", "legacy", "legacy", "admin"),
                ], imported
                with connect(addresses["pgwire"], "legacy", LEGACY_PASSWORD) as session:
                    assert session.execute("select count(*) from trips").fetchall() == [
                        (2,)
                    ]
                # Several users share one HTTP listener.
                status, body = http(
                    addresses["http"], legacy_token, "select id from trips"
                )
                assert status == 200, (status, body)
                status, body = http(addresses["http"], token, "select id from trips")
                assert status == 200, (status, body)

                accounts = host.execute(
                    "select user, role, has_password, tokens::int, tenant "
                    "from duckflight_accounts() order by user"
                ).fetchall()
                assert accounts == [
                    ("alice", "admin", True, 0, None),
                    ("bob", "writer", True, 1, None),
                    ("ci", "writer", False, 1, None),
                    ("legacy", "admin", True, 0, None),
                ], accounts

                # The store keeps verifiers and digests, never the secrets.
                stored = b"".join(path.read_bytes() for path in store.parent.iterdir())
                for secret in (admin_password, bob_password, token, legacy_token):
                    assert secret.encode() not in stored

                for label, address in addresses.items():
                    host.execute(
                        "select * from duckflight_stop(?, ?)",
                        [label.split()[-1], address],
                    )

            # Requests the initialized store cannot honour say why and change nothing.
            content = legacy.read_text()
            for sql, expected in [
                ("select * from duckflight_init('mallory')", "is already initialized"),
                (
                    f"select * from duckflight_user_create('{legacy}', 'mallory')",
                    "entries in",
                ),
                (
                    f"select * from duckflight_token_create('{legacy}', 'mallory')",
                    "entries in",
                ),
                (
                    "select * from duckflight_token_add('nobody', 'mine')",
                    "user does not exist: nobody",
                ),
            ]:
                assert_host_error(cli, extension, fixtures, sql, expected)
            assert legacy.read_text() == content

    print(
        "PASS account store: host setup, serving without a config file, import and refusals"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cli", type=Path)
    parser.add_argument("extension", type=Path)
    args = parser.parse_args()
    run(args.cli, args.extension)

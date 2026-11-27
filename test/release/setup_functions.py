#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2,<4", "pyarrow>=18,<24"]
# ///
"""Verify the SQL setup functions in the actual bundled extension.

A host creates a token and two users through SQL. The generated secrets must authenticate,
must not be stored, and the config the functions write must be one the listeners accept.
"""

from __future__ import annotations

import argparse
import stat
import tempfile
import tomllib
from pathlib import Path

import psycopg
from http_quack_smoke import request, test_host

REFUSAL = "only be called from the process that owns the database"


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
    return request(f"http://{address}/streaming-query", {"query": sql}, token, None)


def assert_host_error(cli: Path, extension: Path, sql: str, parameters: list, expected: str) -> None:
    """The test host stops at the first error, so each failing statement gets its own."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            with test_host(cli, Path(directory), extension, None) as host:
                host.execute(sql, parameters).fetchall()
        except RuntimeError as error:
            if expected not in str(error):
                raise AssertionError(f"expected {expected!r}, got: {error}") from error
            return
    raise AssertionError(f"expected {expected!r} from: {sql}")


def run(cli: Path, extension: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        fixtures = Path(directory)
        config = fixtures / "duckflight.toml"
        password_file = fixtures / "bob.password"
        password_file.write_text("correct horse battery staple\n")
        # Operator-written content the functions must leave alone.
        preface = "# analytics listeners\nshutdown_grace_secs = 1\n"
        config.write_text(preface)

        with test_host(cli, fixtures, extension, None) as host:
            name, reader = host.execute(
                "select * from duckflight_token_create(?, 'reader')", [str(config)]
            ).fetchone()
            assert name == "reader" and len(reader) >= 40, (name, reader)
            _, writer = host.execute(
                "select * from duckflight_token_create(?, 'writer', "
                "scopes := 'query:execute, query:mutate, transaction:manage')",
                [str(config)],
            ).fetchone()
            _, alice_password = host.execute(
                "select * from duckflight_user_create(?, 'alice')", [str(config)]
            ).fetchone()
            assert host.execute(
                "select * from duckflight_user_create(?, 'bob', password_file := ?)",
                [str(config), str(password_file)],
            ).fetchone() == ("bob", None)
            assert host.execute(
                "select users::int, tokens::int, oidc_providers::int, tls, acl "
                "from duckflight_config_check(?)",
                [str(config)],
            ).fetchone() == (2, 2, 0, False, False)

            # Only verifiers are stored, in a file nobody else can read.
            content = config.read_text()
            assert content.startswith(preface), content
            for secret in (reader, writer, alice_password, "correct horse battery staple"):
                assert secret not in content
            assert stat.S_IMODE(config.stat().st_mode) == 0o600
            parsed = tomllib.loads(content)
            assert parsed["tokens"]["reader"]["scopes"] == ["query:execute", "transaction:manage"]
            assert sorted(parsed["users"]) == ["alice", "bob"]

            # HTTP serves one identity per listener, so each token gets its own.
            addresses = {
                label: host.execute(
                    "select address from duckflight_serve(?, '127.0.0.1:0', config_file := ?)",
                    [protocol, str(config)],
                ).fetchone()[0]
                for label, protocol in [
                    ("reader", "http"),
                    ("writer", "http"),
                    ("pgwire", "pgwire"),
                ]
            }
            host.execute("create table trips as select 1 as id")

            status, _ = http(addresses["reader"], reader, "select id from trips")
            assert status == 200, status
            status, _ = http(addresses["reader"], reader, "insert into trips values (2)")
            assert status == 403, status
            status, _ = http(addresses["writer"], writer, "insert into trips values (3)")
            assert status == 200, status
            status, _ = http(addresses["reader"], reader + "x", "select id from trips")
            assert status == 401, status

            for user, password in [
                ("alice", alice_password),
                ("bob", "correct horse battery staple"),
            ]:
                with connect(addresses["pgwire"], user, password) as connection:
                    assert connection.execute(
                        "select id from trips order by id"
                    ).fetchall() == [(1,), (3,)]
            try:
                connect(addresses["pgwire"], "alice", "correct horse battery staple").close()
            except psycopg.OperationalError:
                pass
            else:
                raise AssertionError("alice authenticated with another user's password")

            # A client cannot mint credentials for itself.
            with connect(addresses["pgwire"], "alice", alice_password) as connection:
                for sql in (
                    f"select * from duckflight_token_create('{config}', 'mine')",
                    f"select * from duckflight_user_create('{config}', 'mine')",
                    f"select * from duckflight_config_check('{config}')",
                ):
                    try:
                        outcome = repr(connection.execute(sql).fetchall())
                    except psycopg.Error as error:
                        outcome = str(error)
                    assert REFUSAL in outcome, outcome
            assert config.read_text() == content

            for label, protocol in [("reader", "http"), ("writer", "http"), ("pgwire", "pgwire")]:
                host.execute("select * from duckflight_stop(?, ?)", [protocol, addresses[label]])

        # Requests that cannot be honoured say why and change nothing.
        for sql, parameters, expected in [
            (
                "select * from duckflight_token_create(?, 'reader')",
                [str(config)],
                "tokens.reader already exists",
            ),
            (
                "select * from duckflight_user_create(?, 'alice')",
                [str(config)],
                "users.alice already exists",
            ),
            (
                "select * from duckflight_token_create(?, 'other', scopes := 'query:everything')",
                [str(config)],
                "unsupported scope",
            ),
            (
                "select * from duckflight_config_check(?)",
                [str(fixtures / "missing.toml")],
                "Failed to read DuckFlight config",
            ),
        ]:
            assert_host_error(cli, extension, sql, parameters, expected)
        assert config.read_text() == content

    print("PASS setup functions: generated credentials authenticate and are never stored")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cli", type=Path)
    parser.add_argument("extension", type=Path)
    args = parser.parse_args()
    run(args.cli, args.extension)

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.6", "pyarrow>=18,<24", "protobuf", "psycopg[binary]>=3.2,<4"]
# ///
"""Verify that server functions are host-only in the actual bundled extension.

Starting, stopping and listing servers belongs to the process that owns the database. A session
that arrives through any listener is refused, however the statement reaches the function, and the
host keeps full control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import tempfile
from pathlib import Path

import host_sessions
import psycopg
from http_quack_smoke import request, test_host
from quack_client import WRITE_SCOPES, QuackClient

REFUSAL = "only be called from the process that owns the database"
SERVE_FUNCTIONS = {"http": "http", "quack": "quack", "pgwire": "pg", "flight": "flight"}


def write_config(path: Path, token: str) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", host_sessions.PASSWORD.encode(), salt, 10_000, dklen=32
    ).hex()
    path.write_text(
        "shutdown_grace_secs = 1\n\n"
        f'[tokens.writer]\nsha256 = "{hashlib.sha256(token.encode()).hexdigest()}"\n'
        f'subject = "writer"\nscopes = {json.dumps(WRITE_SCOPES)}\n\n'
        f'[users.{host_sessions.USERNAME}]\npassword_hash = "{digest}"\n'
        f"salt = {list(salt)}\niterations = 10000\n"
    )
    path.chmod(0o600)


def statements(config: Path, quack_address: str) -> dict[str, str]:
    """Every server function, and the indirect routes a statement can take to one."""
    # HTTP caches results by statement text, so each run asks a question it has not asked before.
    nonce = secrets.token_hex(4)
    stop = f"select * from duckflight_stop('quack', '{quack_address}')"
    cases = {
        f"{function} serve": f"select * from duckflight_{function}_serve('127.0.0.1:0', '{config}')"
        for function in SERVE_FUNCTIONS.values()
    }
    cases |= {
        "list": f"select protocol as p_{nonce} from duckflight_servers()",
        "stop": stop,
        "dynamic sql": "select * from query('" + stop.replace("'", "''") + "')",
        "subquery": f"select (select count(*) from duckflight_servers()) as n_{nonce}",
        "common table expression": f"with stopped as ({stop}) select * from stopped",
        "view": f"select * from listener_view where '{nonce}' <> ''",
        "macro": f"select * from listeners() where '{nonce}' <> ''",
    }
    return cases


def assert_refused(label: str, outcome: str) -> None:
    if REFUSAL not in outcome:
        raise AssertionError(f"{label} was not refused: {outcome[:300]}")


def run(cli: Path, extension: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        fixtures = Path(directory)
        token = secrets.token_urlsafe(32)
        config = fixtures / "duckflight.toml"
        write_config(config, token)
        with test_host(cli, fixtures, extension, None) as host:
            address = {
                protocol: host.execute(
                    f"select address from duckflight_{function}_serve('127.0.0.1:0', ?)",
                    [str(config)],
                ).fetchone()[0]
                for protocol, function in SERVE_FUNCTIONS.items()
            }
            host.execute("create view listener_view as select * from duckflight_servers()")
            host.execute(
                "create macro listeners() as table select * from duckflight_servers()"
            )
            cases = statements(config, address["quack"])

            for label, sql in cases.items():
                status, body = request(
                    f"http://{address['http']}/streaming-query", {"query": sql}, token, None
                )
                assert_refused(f"http {label}", f"{status} {body!r}")

            server, port = address["pgwire"].rsplit(":", 1)
            with psycopg.connect(
                host=server,
                port=int(port),
                user=host_sessions.USERNAME,
                password=host_sessions.PASSWORD,
                dbname="memory",
                autocommit=True,
                connect_timeout=5,
            ) as pgwire:
                for label, sql in cases.items():
                    for prepare in (False, True):
                        try:
                            outcome = repr(pgwire.execute(sql, prepare=prepare).fetchall())
                        except psycopg.Error as error:
                            outcome = str(error)
                        assert_refused(f"pgwire {label} (prepare={prepare})", outcome)

            with host_sessions.Session(address["flight"]) as flight:
                for label, sql in cases.items():
                    try:
                        outcome = repr(flight.query(sql))
                    except Exception as error:  # noqa: BLE001 - any Flight error carries the reason
                        outcome = str(error)
                    assert_refused(f"flight {label}", outcome)

            quack = QuackClient(cli, address["quack"], token)
            for label, sql in cases.items():
                try:
                    outcome = quack.run(
                        f"select * from quack_query('quack:{address['quack']}', "
                        f"'{sql.replace(chr(39), chr(39) * 2)}', token := '{token}', "
                        "disable_ssl := true)"
                    )
                except Exception as error:  # noqa: BLE001 - the client reports the reason as text
                    outcome = str(error)
                # Quack also restricts a session to its own statement shapes.
                if "outside its fixed capability" not in outcome:
                    assert_refused(f"quack {label}", outcome)

            # The status function reports no listener details and stays available.
            status, _ = request(
                f"http://{address['http']}/streaming-query",
                {"query": "select loaded from duckflight_core_status()"},
                token,
                None,
            )
            assert status == 200, status

            # Nothing a listener session tried took effect, and the host is unaffected.
            listed = host.execute(
                "select protocol, address from duckflight_servers() order by protocol"
            ).fetchall()
            assert listed == sorted(address.items()), listed
            assert host.execute("select count(*) from listener_view").fetchone() == (4,)
            for protocol, listener in address.items():
                host.execute("select * from duckflight_stop(?, ?)", [protocol, listener])
            assert host.execute("select * from duckflight_servers()").fetchall() == []

    print("PASS server functions are host-only over HTTP, PgWire, Flight SQL and Quack")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cli", type=Path)
    parser.add_argument("extension", type=Path)
    args = parser.parse_args()
    run(args.cli, args.extension)

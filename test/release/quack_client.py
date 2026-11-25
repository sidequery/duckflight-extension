#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=18,<24"]
# ///
"""Drive the Quack listener with DuckDB's official quack extension as the client.

A second DuckDB process installs `quack`, attaches the DuckFlight host as a catalog and
runs the statements a user would type. Each one reaches the listener in the SQL the
client renders, which is the part hand-written protocol tests do not exercise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from http_quack_smoke import test_host

READ_SCOPES = ["query:execute", "transaction:manage"]
WRITE_SCOPES = ["query:execute", "query:mutate", "transaction:manage"]
# The CLI's agent mode, enabled by agent environment variables, plans statements a
# second time for cost estimates, which runs remote DDL twice.
CLIENT_ENVIRONMENT = dict(os.environ, DUCKDB_AGENT_MODE="0")


class ClientError(Exception):
    pass


class QuackClient:
    """One-shot DuckDB CLI sessions that attach the listener as catalog `remote`."""

    def __init__(self, cli: Path, address: str, token: str) -> None:
        self.cli = cli
        self.attach = (
            "load quack;\n"
            f"attach 'quack:{address}' as remote "
            f"(type quack, token '{token}', disable_ssl true);\n"
        )

    def run(self, sql: str) -> list[dict]:
        """Run `sql` after attaching and return the rows of its last statement."""
        result = subprocess.run(
            [str(self.cli), "-no-init", "-batch", ":memory:"],
            input=f".bail on\n{self.attach}.mode json\n{sql};\n",
            check=False,
            env=CLIENT_ENVIRONMENT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise ClientError(result.stderr.strip() or result.stdout.strip())
        output = result.stdout.strip()
        if not output:
            return []
        # Each statement prints one JSON array; the last one is the answer.
        return json.loads(
            output[output.rindex("\n[") + 1 :] if "\n[" in output else output
        )

    def rejects(self, sql: str, expected: str) -> None:
        try:
            self.run(sql)
        except ClientError as error:
            if expected not in str(error):
                raise AssertionError(
                    f"expected {expected!r} for {sql!r}, got: {error}"
                ) from error
            return
        raise AssertionError(f"statement was not rejected: {sql}")


def write_config(path: Path, tokens: dict[str, tuple[str, list[str]]]) -> None:
    sections = []
    for name, (token, scopes) in tokens.items():
        sections.append(
            f"[tokens.{name}]\n"
            f'sha256 = "{hashlib.sha256(token.encode()).hexdigest()}"\n'
            f'subject = "{name}"\n'
            f"scopes = {json.dumps(scopes)}\n"
        )
    path.write_text("shutdown_grace_secs = 1\n" + "\n".join(sections))
    path.chmod(0o600)


def check_read_only(client: QuackClient) -> None:
    rows = client.run("select * from remote.main.trips order by id")
    assert rows == [{"id": 1, "city": "Lisbon"}, {"id": 2, "city": "Osaka"}], rows
    assert client.run("select count(*) as n from remote.main.trips") == [{"n": 2}]
    grouped = client.run(
        "select city, count(*) as n from remote.main.trips "
        "where id > 1 group by city order by 1"
    )
    assert grouped == [{"city": "Osaka", "n": 1}], grouped
    joined = client.run(
        "select t.id, l.label from remote.main.trips t "
        "join remote.main.labels l using (id) order by t.id limit 5"
    )
    assert joined == [{"id": 1, "label": "first"}], joined
    filtered = client.run(
        "select upper(city) as city from remote.main.trips "
        "where city like 'L%' and id in (1, 2)"
    )
    assert filtered == [{"city": "LISBON"}], filtered
    client.rejects(
        "insert into remote.main.trips values (9, 'Nope')",
        "outside its fixed capability",
    )


def check_writes(client: QuackClient, host) -> None:
    for statement in (
        "insert into remote.main.trips values (3, 'Quito')",
        "insert into remote.main.trips values (4, 'Oslo'), (5, 'Rome')",
        "insert into remote.main.trips (city, id) values ('Bern', 6)",
        "insert into remote.main.trips by name select 7 as id, 'Lima' as city",
        "insert into remote.main.trips select 8, 'Riga'",
        "insert into remote.main.labels values (1, 'duplicate') on conflict do nothing",
        "insert or replace into remote.main.labels values (2, 'second')",
        "update remote.main.trips set city = 'Kyoto' where id = 2",
        "delete from remote.main.trips where id = 5",
        "create table remote.main.copied as select 1 as x",
    ):
        client.run(statement)
    returned = client.run(
        "insert into remote.main.trips values (9, 'Cork') returning id"
    )
    assert returned == [{"id": 9}], returned

    trips = host.execute("select id, city from trips order by id").fetchall()
    assert trips == [
        (1, "Lisbon"),
        (2, "Kyoto"),
        (3, "Quito"),
        (4, "Oslo"),
        (6, "Bern"),
        (7, "Lima"),
        (8, "Riga"),
        (9, "Cork"),
    ], trips
    labels = host.execute("select id, label from labels order by id").fetchall()
    assert labels == [(1, "first"), (2, "second")], labels
    assert host.execute("select x from copied").fetchall() == [(1,)]


# Rows that exist only on the client, so the insert has to stream them to the server.
LOCAL_ROWS = (
    "create temp table staged as select i::integer as id, 'city ' || i as city, "
    "(i / 4)::decimal(10, 2) as amount, [i::varchar, 'tag'] as tags "
    "from range({rows}) t(i)"
)
# Small batches sent with random delays reach the server out of order.
MANY_BATCHES = (
    "set quack_target_batch_bytes = 16384; set quack_debug_send_delay_ms = 20"
)


def check_local_rows(client: QuackClient, reader: QuackClient, host) -> None:
    host.execute(
        "create table loaded "
        "(id integer, city varchar, amount decimal(10, 2), tags varchar[])"
    )

    def loaded() -> int:
        return host.execute("select count(*) from loaded").fetchone()[0]

    def out_of_order() -> int:
        # Each load holds ids 0..n in order, so an id below its predecessor either
        # starts the next load or is a row out of place.
        return host.execute(
            "select count(*) from (select id, lag(id) over (order by rowid) as previous "
            "from loaded) where id < previous and id <> 0"
        ).fetchone()[0]

    staged = LOCAL_ROWS.format(rows=5000)
    client.run(f"{staged}; insert into remote.main.loaded select * from staged")
    assert loaded() == 5000, loaded()
    sample = host.execute(
        "select city, amount, tags from loaded where id = 4999"
    ).fetchone()
    assert sample[0] == "city 4999" and sample[2] == ["4999", "tag"], sample
    assert str(sample[1]) == "1249.75", sample

    client.run(
        f"{staged}; insert into remote.main.loaded select * from staged where false"
    )
    assert loaded() == 5000, loaded()

    client.run(
        f"{staged}; {MANY_BATCHES}; insert into remote.main.loaded select * from staged"
    )
    assert loaded() == 10000, loaded()
    assert out_of_order() == 0, out_of_order()

    # A retried batch is applied once, and a lost batch fails the whole statement.
    client.run(
        f"{staged}; {MANY_BATCHES}; set quack_debug_duplicate_sends = 1; "
        "insert into remote.main.loaded select * from staged"
    )
    assert loaded() == 15000, loaded()
    client.rejects(
        f"{staged}; {MANY_BATCHES}; set quack_debug_drop_batch = 2; "
        "insert into remote.main.loaded select * from staged",
        "batches",
    )
    assert loaded() == 15000, loaded()

    client.run(
        f"{staged}; begin; insert into remote.main.loaded select * from staged; rollback"
    )
    assert loaded() == 15000, loaded()
    client.run(
        f"{staged}; begin; insert into remote.main.loaded select * from staged; commit"
    )
    assert loaded() == 20000, loaded()
    assert out_of_order() == 0, out_of_order()

    client.run(f"{staged}; create table remote.main.from_local as select * from staged")
    assert host.execute("select count(*), max(id) from from_local").fetchone() == (
        5000,
        4999,
    )

    # A failed stream leaves nothing behind and the attachment stays usable.
    host.execute("create table keyed (id integer primary key)")
    client.rejects(
        "create temp table repeated as select 1 as id from range(10); "
        "insert into remote.main.keyed select * from repeated",
        "constraint violation",
    )
    assert host.execute("select count(*) from keyed").fetchone() == (0,)
    client.run("insert into remote.main.keyed values (1)")
    assert host.execute("select count(*) from keyed").fetchone() == (1,)

    reader.rejects(
        f"{staged}; insert into remote.main.loaded select * from staged", "read-only"
    )
    assert loaded() == 20000, loaded()


def run(cli: Path, extension: Path | None, repository: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="duckflight-quack-client-") as directory:
        fixtures = Path(directory)
        read_token = secrets.token_urlsafe(32)
        write_token = secrets.token_urlsafe(32)
        config = fixtures / "duckflight.toml"
        write_config(
            config,
            {
                "reader": (read_token, READ_SCOPES),
                "writer": (write_token, WRITE_SCOPES),
            },
        )
        subprocess.run(
            [str(cli), "-no-init", "-batch", ":memory:"],
            input="install quack;\n",
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        with test_host(cli, fixtures, extension, repository) as host:
            assert host.execute(
                "select loaded from duckflight_core_status()"
            ).fetchone() == (True,)
            host.execute(
                "create table trips as select * from "
                "(values (1, 'Lisbon'), (2, 'Osaka')) t(id, city); "
                "create table labels (id integer primary key, label varchar); "
                "insert into labels values (1, 'first')"
            )
            address = host.execute(
                "select address from duckflight_quack_serve('127.0.0.1:0', ?)",
                [str(config)],
            ).fetchone()[0]
            try:
                # A read-only token must be enough to attach and query.
                reader = QuackClient(cli, address, read_token)
                writer = QuackClient(cli, address, write_token)
                check_read_only(reader)
                check_writes(writer, host)
                check_local_rows(writer, reader, host)
            finally:
                host.execute("select * from duckflight_stop('quack', ?)", [address])
    print(
        "PASS DuckDB quack client: read-only attach and queries, inserts, updates, "
        "deletes and DDL through an attached catalog, and streamed local rows"
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

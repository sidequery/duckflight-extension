"""Real-client ClickBench contract tests; run after benchmarks/clickbench/install.

uv run --project benchmarks/clickbench --frozen test/test_clickbench.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "benchmarks/clickbench"
SPEC = importlib.util.spec_from_file_location("clickbench", ROOT / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class ClickBenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="duckflight-clickbench-")
        cls.state = Path(cls.directory.name)
        shutil.copy2(ROOT / ".state/duckdb", cls.state / "duckdb")
        shutil.copyfile(
            ROOT / ".state/duckflight.duckdb_extension",
            cls.state / "duckflight.duckdb_extension",
        )
        cls.queries = (ROOT / "queries.sql").read_text().splitlines()
        with harness.local_database(cls.state) as database:
            database.execute((ROOT / "create.sql").read_text())
            columns = database.execute("describe hits").fetchall()
            expressions = []
            # Seven distinct rows with unequal frequencies avoid LIMIT-boundary
            # ties; the full dataset still needs its own correctness validation.
            for name, kind, *_ in columns:
                if kind == "DATE":
                    value = "date '2013-07-14' + k::integer"
                elif kind == "TIMESTAMP":
                    value = "timestamp '2013-07-14' + k * interval '1 minute'"
                elif kind == "VARCHAR":
                    value = "'Google https://google.test/query-' || k"
                elif name == "CounterID":
                    value = "62"
                elif name in ("IsRefresh", "DontCountHits", "IsDownload"):
                    value = "0"
                else:
                    value = "k"
                expressions.append(f'{value} as "{name}"')
            database.execute(
                "insert into hits select "
                + ", ".join(expressions)
                + " from range(7) keys(k), lateral range((k + 1) * 101) copies(i)"
            )
            cls.expected = [database.execute(sql).fetchall() for sql in cls.queries]
        result = cls.hook("start")
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.hook("stop")
        cls.directory.cleanup()

    @classmethod
    def hook(cls, name, sql=""):
        return subprocess.run(
            [str(ROOT / name)],
            input=sql,
            capture_output=True,
            text=True,
            env={**os.environ, "DUCKFLIGHT_BENCH_STATE": str(cls.state)},
            timeout=30,
            check=False,
        )

    def test_all_43_queries_match_direct_duckdb(self):
        self.assertEqual(len(self.queries), 43)
        with harness.connect(self.state) as connection, connection.cursor() as cursor:
            for number, (sql, expected) in enumerate(
                zip(self.queries, self.expected), 1
            ):
                with self.subTest(query=number):
                    cursor.execute(sql)
                    self.assertEqual(Counter(cursor.fetchall()), Counter(expected))

    def test_direct_flight_execution_matches_all_43_queries(self):
        with harness.connect(self.state) as connection:
            for number, (sql, expected) in enumerate(
                zip(self.queries, self.expected), 1
            ):
                with self.subTest(query=number):
                    with harness.query_result(connection, sql) as table:
                        rows = list(
                            zip(*(column.to_pylist() for column in table.columns))
                        )
                    self.assertEqual(Counter(rows), Counter(expected))

    def test_query_hook_emits_complete_result_and_seconds(self):
        result = self.hook("query", "select i from range(10001) t(i) order by i")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["i", *map(str, range(10001))])
        self.assertGreater(float(result.stderr.strip()), 0)

    def test_query_failure_is_nonzero_without_success_timing(self):
        result = self.hook("query", "select * from missing_clickbench_table")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing_clickbench_table", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_concurrent_clients(self):
        def run(_):
            result = self.hook("query", "select count(*) as n from hits")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["n", "2828"])

        with ThreadPoolExecutor(max_workers=10) as workers:
            list(workers.map(run, range(10)))

    def test_restart_replaces_host_and_preserves_data(self):
        before = json.loads((self.state / "process.json").read_text())
        stopped = self.hook("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertFalse(harness.process_alive(before["pid"]))
        # A successful stop must permit immediate direct access to the same
        # database, not merely remove the Flight discovery file.
        with harness.local_database(self.state) as database:
            self.assertEqual(
                database.execute("select count(*) from hits").fetchone(), (2828,)
            )
        self.assertNotEqual(self.hook("check").returncode, 0)
        result = self.hook("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads((self.state / "process.json").read_text())
        self.assertNotEqual(before["nonce"], after["nonce"])
        self.assertEqual(self.hook("check").returncode, 0)
        self.assertEqual(
            self.hook("query", "select count(*) as n from hits").stdout, "n\n2828\n"
        )
        self.assertGreater(int(self.hook("data-size").stdout), 0)

    def test_load_reopens_latest_storage_and_refuses_existing_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (root / "create.sql").write_text(
                "create table hits (EventDate date, EventTime timestamp, "
                "ClientEventTime timestamp, LocalEventTime timestamp)"
            )
            with harness.duckdb.connect() as database:
                database.execute(
                    "copy (select 15900 as EventDate, 42 as EventTime, "
                    "43 as ClientEventTime, 44 as LocalEventTime) to "
                    + harness.sql_string(root / "hits.parquet")
                    + " (format parquet)"
                )
            # The upstream runner starts the host before loading, so its
            # database can already exist while the hits table is still absent.
            with harness.local_database(state):
                pass
            with (
                patch.object(harness, "ROOT", root),
                patch.object(harness, "EXPECTED_ROWS", 1),
                patch.object(harness, "start"),
            ):
                harness.load(state)
                with self.assertRaises(harness.duckdb.CatalogException):
                    harness.load(state)
            with harness.local_database(state) as database:
                self.assertEqual(
                    database.execute("select epoch(EventTime) from hits").fetchall(),
                    [(42.0,)],
                )

    def test_stop_waits_for_database_lock_even_without_pid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            owner = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    """import sys
import duckdb

with duckdb.connect(sys.argv[1]) as database:
    database.execute('create table probe as select 42 as answer')
    print('ready', flush=True)
    sys.stdin.readline()
""",
                    str(state / "hits.db"),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(owner.stdout.readline().strip(), "ready")
                with ThreadPoolExecutor(max_workers=1) as worker:
                    stopped = worker.submit(harness.stop, state)
                    with self.assertRaises(TimeoutError):
                        stopped.result(timeout=0.25)
                    owner.stdin.write("release\n")
                    owner.stdin.flush()
                    stopped.result(timeout=10)
                with harness.local_database(state) as database:
                    self.assertEqual(
                        database.execute("select * from probe").fetchall(), [(42,)]
                    )
            finally:
                owner.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()

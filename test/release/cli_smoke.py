"""Run the release SQL smoke suite in the exact pinned CLI instead of PyPI."""

from __future__ import annotations

import argparse
from pathlib import Path

from cli_host import CLIHost
from duckdb_sqllogictest.parser import SQLLogicParser
from duckdb_sqllogictest.statement.query import Query, SortStyle
from duckdb_sqllogictest.statement.require import Require


def run(cli: Path, extension: Path) -> None:
    test = SQLLogicParser().parse(str(Path(__file__).with_name("duckflight.test")))
    if test is None:
        raise AssertionError("release smoke suite did not parse")
    with CLIHost(cli) as host:
        version = host.execute("pragma version").fetchone()
        if version is None or version[1] != "d54cbbe3df":
            raise AssertionError(f"expected DuckDB final pin d54cbbe3df, got {version}")
        host.execute("load '" + str(extension.resolve()).replace("'", "''") + "'")
        count = 0
        for statement in test.statements:
            if isinstance(statement, Require) and statement.get_parameters() == [
                "duckflight"
            ]:
                continue
            # Fail closed if the smoke suite gains constructs this runner does
            # not implement. The official parser remains the syntax authority.
            if (
                not isinstance(statement, Query)
                or statement.decorators
                or statement.connection_name
                or statement.sortstyle != SortStyle.NO_SORT
                or statement.expected_result is None
            ):
                raise AssertionError(f"unsupported smoke statement: {statement.header}")
            rows = host.execute("\n".join(statement.lines)).fetchall()
            actual = ["\t".join(str(value) for value in row) for row in rows]
            expected = statement.expected_result.lines
            if actual != expected:
                raise AssertionError(
                    f"line {statement.query_line}: expected {expected!r}, got {actual!r}"
                )
            count += 1
    print(f"PASS {count} release smoke queries on {version}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", type=Path)
    parser.add_argument("extension", type=Path)
    args = parser.parse_args()
    run(args.cli, args.extension)

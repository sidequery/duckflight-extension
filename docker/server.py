"""Own the DuckDB connection and extension listeners for the container lifetime."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

import duckdb


def main() -> None:
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())

    config_file = Path(os.environ["DUCKFLIGHT_CONFIG"])
    if not config_file.is_file():
        raise ValueError(f"mount authentication/TLS config at {config_file}")
    listeners = []
    for protocol, function, variable in (
        ("pgwire", "duckflight_pg_serve", "DUCKFLIGHT_PG_ADDRESS"),
        ("flight", "duckflight_flight_serve", "DUCKFLIGHT_FLIGHT_ADDRESS"),
    ):
        address = os.environ.get(variable, "")
        if address:
            listeners.append((protocol, function, address))
    if not listeners:
        raise ValueError("enable at least one listener address")

    settings = {"allow_unsigned_extensions": "true"}
    for variable, setting in (
        ("DUCKFLIGHT_MEMORY_LIMIT", "memory_limit"),
        ("DUCKFLIGHT_THREADS", "threads"),
        ("DUCKFLIGHT_TEMP_DIRECTORY", "temp_directory"),
    ):
        if os.environ.get(variable):
            settings[setting] = os.environ[variable]

    connection = duckdb.connect(os.environ["DUCKFLIGHT_DATABASE"], config=settings)
    started = []
    try:
        connection.execute("load '/opt/duckflight/duckflight.duckdb_extension'")
        loaded, _, detail = connection.execute(
            "select * from duckflight_core_status()"
        ).fetchone()
        if not loaded:
            raise RuntimeError(f"bundled DuckFlight core unavailable: {detail}")
        init_file = os.environ.get("DUCKFLIGHT_INIT_SQL")
        if init_file:
            connection.execute(Path(init_file).read_text())
        for protocol, function, address in listeners:
            bound = connection.execute(
                f"select address from {function}(?, ?)", [address, str(config_file)]
            ).fetchone()[0]
            started.append((protocol, bound))
            print(f"DuckFlight {protocol} listening on {bound}", flush=True)
        print("DuckFlight ready", flush=True)
        stopped.wait()
    finally:
        # Stop every listener, even if startup or another stop failed, before closing
        # the owning connection and flushing the database.
        try:
            for protocol, address in reversed(started):
                try:
                    connection.execute(
                        "select * from duckflight_stop(?, ?)", [protocol, address]
                    ).fetchall()
                except duckdb.Error as error:
                    print(f"failed to stop {protocol}: {error}", file=sys.stderr)
        finally:
            connection.close()


if __name__ == "__main__":
    main()

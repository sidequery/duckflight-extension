"""Persistent DuckDB CLI host for testing release pins without a matching wheel."""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Self


class CLIHost:
    def __init__(self, executable: Path) -> None:
        # This context manager owns the file until the child has exited.
        self.stderr = tempfile.TemporaryFile(mode="w+t")  # noqa: SIM115
        try:
            self.process = subprocess.Popen(
                [
                    str(executable.resolve()),
                    "-no-init",
                    "-unsigned",
                    "-batch",
                    ":memory:",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                text=True,
                bufsize=1,
            )
        except OSError:
            self.stderr.close()
            raise
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.rows: list[tuple] = []
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        self._write(".bail on\n.mode json\n")

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _write(self, commands: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(commands)
        self.process.stdin.flush()

    def _error(self) -> str:
        self.stderr.seek(0)
        return self.stderr.read()

    def execute(self, sql: str, parameters: list | None = None) -> Self:
        if parameters is not None:
            # Let DuckDB bind placeholders rather than rewriting SQL text.
            values = []
            for value in parameters:
                if isinstance(value, str):
                    values.append("'" + value.replace("'", "''") + "'")
                elif isinstance(value, int):
                    values.append(str(value))
                else:
                    raise TypeError(f"unsupported CLI test parameter: {type(value)}")
            sql = (
                f"prepare duckflight_cli_test as {sql};\n"
                f"execute duckflight_cli_test({', '.join(values)});\n"
                "deallocate duckflight_cli_test"
            )
        marker = "duckflight_cli_query_complete"
        try:
            self._write(f"{sql};\n.print {marker}\n")
        except BrokenPipeError as error:
            raise RuntimeError(self._error()) from error
        output = []
        while True:
            try:
                line = self.lines.get(timeout=30)
            except queue.Empty as error:
                raise TimeoutError(f"DuckDB CLI query timed out: {sql}") from error
            if line is None:
                raise RuntimeError(f"DuckDB CLI exited: {self._error()}")
            if line.strip() == marker:
                break
            output.append(line)
        raw = "".join(output).strip()
        self.rows = [tuple(row.values()) for row in json.loads(raw)] if raw else []
        return self

    def fetchone(self) -> tuple | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple]:
        rows, self.rows = self.rows, []
        return rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=1)
        assert self.process.stdout is not None
        self.process.stdout.close()
        self.stderr.close()

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "adbc-driver-flightsql",
#     "psycopg[binary]",
#     "psycopg2-binary",
#     "pyarrow",
#     "sqlalchemy",
# ]
# ///
"""Run the code snippets in the documentation against a real DuckFlight host.

A fenced block opts in through its info string:

    ```sql doctest=host    SQL run in the DuckDB process that loaded the extension
    ```sql doctest=pg      SQL run as `alice` through the PostgreSQL listener
    ```python doctest      a Python program
    ```sh doctest          a shell script

Each documentation file is one scenario. Its tagged blocks run top to bottom in a
fresh directory against a fresh in-memory host, so a file must start the listeners
its later snippets use. Before the host starts, the runner makes sure the user
`alice` and a bearer token exist, creating whichever the file did not create itself.
Snippets read `DUCKFLIGHT_PASSWORD`, `PGPASSWORD` and `DUCKFLIGHT_TOKEN` from the
environment.

Three things differ from what a reader types:

* the extension is already loaded, so `install duckflight ...` and `load duckflight`
  lines in host blocks are skipped;
* the credential helper's release URL is replaced by this checkout's copy, and
  `duckflight-auth` is defined as a shell function that runs it;
* password and token prompts are answered automatically.

Relative links and heading anchors in every Markdown file are checked first, and the
helper URL must be pinned to the version in Cargo.toml.
"""

from __future__ import annotations

import argparse
import os
import pty
import re
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "test" / "release"))

from cli_host import CLIHost

HELPER = REPO / "scripts" / "duckflight_auth.py"
HELPER_URL = re.compile(
    r"https://raw\.githubusercontent\.com/sidequery/duckflight-extension/"
    r"(?P<ref>[^/\s]+)/scripts/duckflight_auth\.py"
)
FENCE = re.compile(r"^```(?P<language>[a-z]+)?(?P<attributes>[^`]*)$")
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\((?P<target>[^)\s]+)\)")
PG_ADDRESS = "127.0.0.1:5433"
OPTIONAL_TOOLS = ("psql", "curl", "openssl")
BLOCK_TIMEOUT_SECONDS = 120


class SnippetError(Exception):
    pass


@dataclass
class Block:
    path: Path
    line: int
    language: str
    role: str
    code: str

    @property
    def label(self) -> str:
        return (
            f"{self.path.relative_to(REPO)}:{self.line} ({self.language} {self.role})"
        )


def documentation_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\n")
    return sorted(
        REPO / name
        for name in listed
        if name and not name.startswith("extension-ci-tools/")
    )


def parse_blocks(path: Path) -> tuple[list[Block], list[tuple[int, str]]]:
    """Return the tagged blocks and the prose lines (those outside any fence)."""
    blocks: list[Block] = []
    prose: list[tuple[int, str]] = []
    opened: tuple[int, str, str | None] | None = None
    body: list[str] = []
    for number, line in enumerate(path.read_text().split("\n"), start=1):
        fence = FENCE.match(line.strip())
        if opened is None:
            if fence:
                attributes = fence["attributes"].split()
                role = next(
                    (
                        "run" if item == "doctest" else item.removeprefix("doctest=")
                        for item in attributes
                        if item == "doctest" or item.startswith("doctest=")
                    ),
                    None,
                )
                opened = (number, fence["language"] or "", role)
                body = []
            else:
                prose.append((number, line))
        elif line.strip() == "```":
            start, language, role = opened
            if role is not None:
                blocks.append(Block(path, start, language, role, "\n".join(body)))
            opened = None
        else:
            body.append(line)
    if opened is not None:
        raise SnippetError(f"{path}: unclosed code fence at line {opened[0]}")
    return blocks, prose


def heading_anchors(path: Path) -> set[str]:
    """Anchors GitHub generates for a file's headings."""
    _, prose = parse_blocks(path)
    anchors: set[str] = set()
    for _, line in prose:
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if not heading:
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading[1]).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", text).replace(" ", "-")
        candidate, suffix = slug, 0
        while candidate in anchors:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        anchors.add(candidate)
    return anchors


def check_links(files: list[Path]) -> list[str]:
    problems = []
    for path in files:
        _, prose = parse_blocks(path)
        for number, line in prose:
            for match in LINK.finditer(line):
                target = match["target"]
                if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue
                file_part, _, anchor = target.partition("#")
                destination = (path.parent / file_part).resolve() if file_part else path
                where = f"{path.relative_to(REPO)}:{number}"
                if not destination.exists():
                    problems.append(f"{where}: missing link target {target}")
                elif (
                    anchor
                    and destination.suffix == ".md"
                    and anchor not in heading_anchors(destination)
                ):
                    problems.append(f"{where}: missing anchor {target}")
    return problems


def check_helper_pin(files: list[Path]) -> list[str]:
    version = tomllib.loads((REPO / "Cargo.toml").read_text())["package"]["version"]
    problems = []
    for path in files:
        for number, line in enumerate(path.read_text().split("\n"), start=1):
            for match in HELPER_URL.finditer(line):
                if match["ref"] != f"v{version}":
                    problems.append(
                        f"{path.relative_to(REPO)}:{number}: helper URL is pinned to "
                        f"{match['ref']}, expected v{version}"
                    )
    return problems


class Host(CLIHost):
    def run_script(self, sql: str) -> None:
        """Run several statements, discarding their results."""
        marker = "duckflight_docs_block_complete"
        try:
            self._write(f"{sql}\n;\n.print {marker}\n")
        except BrokenPipeError as error:
            raise SnippetError(self._error()) from error
        while True:
            line = self.lines.get(timeout=BLOCK_TIMEOUT_SECONDS)
            if line is None:
                raise SnippetError(self._error().strip() or "DuckDB exited")
            if line.strip() == marker:
                return


class Scenario:
    def __init__(self, arguments: argparse.Namespace, directory: Path) -> None:
        self.arguments = arguments
        self.directory = directory
        self.password = secrets.token_urlsafe(18)
        self.token: str | None = None
        self.host: Host | None = None
        self.environment = dict(
            os.environ,
            DUCKFLIGHT_PASSWORD=self.password,
            PGPASSWORD=self.password,
            # Shell blocks run on a terminal; keep psql from waiting in a pager.
            PAGER="cat",
            PSQL_PAGER="cat",
        )

    def redact(self, text: str) -> str:
        for secret in (self.password, self.token):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def close(self) -> None:
        if self.host is not None:
            self.host.__exit__(None, None, None)
            self.host = None

    def shell(self, script: str) -> str:
        """Run a shell script on a terminal, answering credential prompts."""
        prelude = (
            f'set -euo pipefail\nduckflight-auth() {{ uv run "{HELPER}" "$@"; }}\n'
        )
        script = HELPER_URL.sub(lambda _: f'"{HELPER}"', script)
        controller, terminal = pty.openpty()
        process = subprocess.Popen(
            ["bash", "-c", prelude + script],
            cwd=self.directory,
            env=self.environment,
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
            start_new_session=True,
        )
        os.close(terminal)
        output = ""
        answered = 0
        deadline = time.monotonic() + BLOCK_TIMEOUT_SECONDS
        try:
            while True:
                if time.monotonic() > deadline:
                    process.kill()
                    raise SnippetError("timed out\n" + self.redact(output))
                readable, _, _ = select.select([controller], [], [], 0.2)
                if readable:
                    try:
                        chunk = os.read(controller, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        output += chunk.decode(errors="replace")
                    elif process.poll() is not None:
                        break
                elif process.poll() is not None:
                    break
                self._remember_token(output)
                pending = output[answered:]
                if re.search(r"[Pp]assword[^\n]*: ?$", pending):
                    os.write(controller, self.password.encode() + b"\n")
                    answered = len(output)
                elif re.search(r"Token for [^\n]*: ?$", pending):
                    os.write(controller, (self.token or "").encode() + b"\n")
                    answered = len(output)
        finally:
            os.close(controller)
        status = process.wait()
        self._remember_token(output)
        if status != 0:
            raise SnippetError(f"exit status {status}\n{self.redact(output)}")
        return output

    def _remember_token(self, output: str) -> None:
        """Keep the most recent token printed by `token add`."""
        printed = re.findall(r"^token=(\S+)", output, flags=re.MULTILINE)
        if printed:
            self.token = printed[-1]
            self.environment["DUCKFLIGHT_TOKEN"] = self.token

    def ensure_credentials(self) -> None:
        config = self.directory / "duckflight.toml"
        users = (
            tomllib.loads(config.read_text()).get("users", {})
            if config.exists()
            else {}
        )
        if "alice" not in users:
            self.shell("duckflight-auth user add alice")
        if self.token is None:
            self.shell("duckflight-auth token add docs-runner")

    def ensure_host(self) -> Host:
        if self.host is not None:
            return self.host
        self.ensure_credentials()
        os.chdir(self.directory)
        extension = self.arguments.extension
        self.host = Host(self.arguments.cli, allow_unsigned=extension is not None)
        if extension is None:
            self.host.run_script("install duckflight from community; load duckflight")
        else:
            path = str(extension).replace("'", "''")
            self.host.run_script(f"load '{path}'")
        loaded = self.host.execute(
            "select loaded, detail from duckflight_core_status()"
        )
        row = loaded.fetchone()
        if row is None or row[0] is not True:
            raise SnippetError(f"DuckFlight core did not load: {row}")
        return self.host

    def run_host(self, block: Block) -> None:
        kept = [
            line
            for line in block.code.split("\n")
            if not re.match(r"^\s*(install|load)\s+duckflight\b", line, re.IGNORECASE)
        ]
        self.ensure_host().run_script("\n".join(kept))

    def run_pg(self, block: Block) -> None:
        host = self.ensure_host()
        listeners = host.execute("select protocol from duckflight_servers()").fetchall()
        if ("pgwire",) not in listeners:
            host.execute(
                "select * from duckflight_pg_serve(?, ?)",
                [PG_ADDRESS, "duckflight.toml"],
            )
        address, port = PG_ADDRESS.rsplit(":", 1)
        with psycopg.connect(
            host=address,
            port=int(port),
            user="alice",
            dbname="memory",
            password=self.password,
            autocommit=True,
        ) as connection:
            # Documentation SQL keeps one statement per `;` at the end of a line.
            for statement in re.split(r";[ \t]*(?:--[^\n]*)?(?:\n|$)", block.code):
                if statement.strip():
                    connection.execute(statement)

    def run_python(self, block: Block) -> None:
        result = subprocess.run(
            [sys.executable, "-c", block.code],
            check=False,
            cwd=self.directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=BLOCK_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise SnippetError(self.redact(result.stdout + result.stderr))

    def run(self, block: Block) -> str:
        """Run one block; return "PASS" or a "SKIP ..." reason."""
        if block.language == "sql" and block.role == "host":
            self.run_host(block)
        elif block.language == "sql" and block.role == "pg":
            self.run_pg(block)
        elif block.language == "python" and block.role == "run":
            self.run_python(block)
        elif block.language == "sh" and block.role == "run":
            missing = [
                tool
                for tool in OPTIONAL_TOOLS
                if re.search(rf"(^|\s){tool}\s", block.code) and not shutil.which(tool)
            ]
            if missing and self.arguments.skip_missing_tools:
                return f"SKIP (no {', '.join(missing)})"
            self.shell(block.code)
        else:
            raise SnippetError(
                f"unsupported doctest block: language={block.language!r} "
                f"role={block.role!r}"
            )
        return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("cli", type=Path, nargs="?", help="DuckDB CLI executable")
    parser.add_argument(
        "extension",
        type=Path,
        nargs="?",
        help="extension file to load; omit to install the published Community build",
    )
    parser.add_argument(
        "--links-only",
        action="store_true",
        help="check links and the helper URL pin without starting a host",
    )
    parser.add_argument(
        "--skip-missing-tools",
        action="store_true",
        help=f"skip shell blocks needing an absent tool ({', '.join(OPTIONAL_TOOLS)})",
    )
    arguments = parser.parse_args()
    if arguments.cli is None and not arguments.links_only:
        parser.error("provide the DuckDB CLI path, or pass --links-only")

    files = documentation_files()
    problems = check_links(files) + check_helper_pin(files)
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"checked links in {len(files)} Markdown files")
    if arguments.links_only:
        return 1 if problems else 0

    # Scenarios change directory, so pin both paths to the invocation directory.
    arguments.cli = arguments.cli.resolve()
    if arguments.extension is not None:
        arguments.extension = arguments.extension.resolve()
    failures = len(problems)
    ran = 0
    for path in files:
        blocks, _ = parse_blocks(path)
        if not blocks:
            continue
        with tempfile.TemporaryDirectory(prefix="duckflight-docs-") as directory:
            scenario = Scenario(arguments, Path(directory).resolve())
            try:
                for block in blocks:
                    try:
                        outcome = scenario.run(block)
                    except (
                        SnippetError,
                        psycopg.Error,
                        subprocess.TimeoutExpired,
                    ) as e:
                        failures += 1
                        print(f"FAIL {block.label}\n{scenario.redact(str(e)).rstrip()}")
                        # Later blocks in the file depend on this one.
                        break
                    ran += outcome == "PASS"
                    print(f"{outcome} {block.label}")
            finally:
                scenario.close()
                os.chdir(REPO)
    print(f"{ran} snippets passed, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

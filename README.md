# DuckFlight DuckDB extension

DuckFlight lets PostgreSQL and Arrow clients query DuckDB directly. Load the extension, start a
listener, then connect with `psql`, a PostgreSQL driver, an ADBC Flight SQL client, or DuckDB's
Airport extension. Every client queries the same DuckDB database—there is no second database to run
and no data to copy.

## Authentication setup

One `duckflight.toml` configures network authentication and the shared TLS identity. The repository
includes a dependency-free PEP 723 helper so operators do not need to derive SCRAM fields or bearer
token hashes manually:

```sh
uv run scripts/duckflight_auth.py user add alice --file duckflight.toml
uv run scripts/duckflight_auth.py token add airport --file duckflight.toml
uv run scripts/duckflight_auth.py check --file duckflight.toml
```

The token command creates a read/query plus transaction-management token by default (the minimum
Airport needs for reads), prints the raw value once, and stores only its SHA-256 digest. The config still
contains password-verification material and authentication policy, so handle it as a secret: do not
commit it, publish it in release assets, or bake it into container images. The default root file is
ignored by this repository. See [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) for the complete
schema, transport policy, Airport setup, storage, and rotation guidance.

## SQL API

```sql
-- PostgreSQL wire protocol
select * from duckflight_pg_serve(
  '127.0.0.1:5433', '/path/to/duckflight.toml'
);

-- Arrow Flight SQL / ADBC
select * from duckflight_adbc_serve(
  '127.0.0.1:31337', '/path/to/duckflight.toml'
);

-- Current servers
select * from duckflight_servers();

-- Protocol aliases include pgwire/postgres/postgresql and adbc/flight/flightsql/flight_sql.
select * from duckflight_stop(
  'pgwire', '127.0.0.1:5433'
);
```

| Function | Result | Purpose |
| --- | --- | --- |
| `duckflight_core_status()` | `loaded`, `abi_version`, `detail` | Inspect runtime availability |
| `duckflight_pg_serve(address, config_file)` | `protocol`, `address` | Start PostgreSQL wire protocol |
| `duckflight_adbc_serve(address, config_file)` | `protocol`, `address` | Start Arrow Flight SQL |
| `duckflight_servers()` | `protocol`, `address` | List active endpoints |
| `duckflight_stop(protocol, address)` | `status` | Stop an endpoint |

Both listeners require authentication. Plaintext transport is allowed only when the actual bound
address is loopback; any non-loopback bind refuses to start without the shared `[tls]` certificate
and key. PgWire clients use SCRAM-SHA-256. ADBC Flight SQL clients use the standard username/password
handshake over TLS and receive a bounded session bearer. Airport does not perform that handshake,
so it uses a generated token through DuckDB's Secrets Manager. The exact client setup is documented
in [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md).

## Build and test

The repository follows DuckDB's Rust Community Extension template and pins
`extension-ci-tools` as a submodule. Clone with submodules, then run:

```sh
git submodule update --init --recursive
make configure
make debug
make test_debug
```

`make test_debug` builds an open mock runtime and points `DUCKFLIGHT_CORE_PATH` at it. The mock exists
only to test the public ABI and SQL lifecycle deterministically; it does not implement a database
server. Unit and lint checks are:

```sh
cargo fmt --all -- --check
cargo check --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

The distribution workflow uses DuckDB's reusable build, test, metadata, and packaging matrix for
DuckDB v1.5.5. Generated shared libraries, build trees, and `.duckdb_extension` artifacts are ignored
and must not be committed.

The initial Community release supports Linux and macOS on amd64 and arm64. Windows, WebAssembly,
and Linux musl are explicitly excluded until matching bundled core payloads pass the same release
smoke test; unsupported platforms never fall back to a source checkout or runtime download.

An authorized release build embeds the private core directly into the extension:

```sh
./scripts/build-bundled.sh /path/to/private/duckflight
```

See [docs/BUNDLED_CORE.md](docs/BUNDLED_CORE.md) for the local build and per-platform GitHub Release
asset model. The platform payloads are published in the
[`core-v0.1.0` release](https://github.com/sidequery/duckflight-extension/releases/tag/core-v0.1.0)
and checksum-pinned in `core-assets.lock`.

<details>
<summary>Developing without a bundled core</summary>

Production extensions are self-contained. For public-source development and CI, an unbundled build
can instead load an ABI-compatible core from `DUCKFLIGHT_CORE_PATH`:

```sh
export DUCKFLIGHT_CORE_PATH=/absolute/path/to/libduckflight_core_ffi.dylib
duckdb
```

Use `.so` on Linux. Without a compatible core, `LOAD duckflight` still permits metadata inspection
while server operations return an availability error. Inspect the state with
`select * from duckflight_core_status();`.

</details>

## Community Extension status

This repository is structured for DuckDB Community Extension CI and includes a provisional
`description.yml`. Before an upstream submission, `repo.ref` must identify the public commit to be
built. There is no separately installed runtime: each immutable platform core library is a GitHub
Release asset pinned by exact URL and SHA-256 in `core-assets.lock`. The mandated `make release`
entrypoint fetches, verifies, and embeds it during the build.

## Security

The bundled core executes native code in the DuckDB process. Release inputs must be immutable and
checksum-verified. SQL callers are trusted to manage DuckFlight listeners, just as they are trusted
to operate the DuckDB instance. The core rejects non-loopback listeners without TLS and rejects
listeners with no authentication method. See [SECURITY.md](SECURITY.md) for reporting guidance.

## License

The extension shim, ABI crate, and test mock in this repository are available under the MIT License.
The private core source license is independent. Production release assets must carry an explicit
binary license grant from the core copyright holder that is compatible with the extension's MIT
metadata; the core owner can dual-license the binary without publishing its private source.

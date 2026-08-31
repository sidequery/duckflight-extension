# DuckFlight DuckDB extension

DuckFlight is a DuckDB extension that exposes the database which loaded it through PostgreSQL's
wire protocol and Arrow Flight SQL. This repository contains the open-source DuckDB extension shim
and its versioned C ABI. Production artifacts are platform-specific single-file extensions with the
DuckFlight core embedded in the `.duckdb_extension`; users do not install a sidecar runtime.

## Runtime model

The public source tree remains buildable without private source or binaries. Its development build
can load an ABI-compatible core dynamically, which is how the open mock is exercised in CI:

```sh
export DUCKFLIGHT_CORE_PATH=/absolute/path/to/libduckflight_core_ffi.dylib
duckdb
```

Use `.so` on Linux and `.dll` on Windows. `LOAD duckflight` deliberately succeeds when this variable
is absent or invalid, allowing metadata and function-discovery tooling to inspect a public-source
build. Production builds embed and load the platform core from the extension artifact and ignore
`DUCKFLIGHT_CORE_PATH`. Status details never expose the host library path:

```sql
load duckflight;
select * from duckflight_core_status();
```

Development server operations return a clear error until a compatible runtime is available.

Listener management is capability-protected. Set a non-empty token before DuckDB loads the
extension and supply it to every lifecycle call:

```sh
export DUCKFLIGHT_MANAGEMENT_TOKEN='a-long-random-operator-token'
```

## SQL API

```sql
-- PostgreSQL wire protocol
select * from duckflight_pg_serve(
  '127.0.0.1:5433', '/path/to/users.toml', 'a-long-random-operator-token'
);

-- Arrow Flight SQL / ADBC
select * from duckflight_adbc_serve(
  '127.0.0.1:31337', '/path/to/users.toml', 'a-long-random-operator-token'
);

-- Current servers
select * from duckflight_servers('a-long-random-operator-token');

-- Protocol aliases include pgwire/postgres/postgresql and adbc/flight/flightsql/flight_sql.
select * from duckflight_stop(
  'pgwire', '127.0.0.1:5433', 'a-long-random-operator-token'
);
```

| Function | Result | Purpose |
| --- | --- | --- |
| `duckflight_core_status()` | `loaded`, `abi_version`, `detail` | Inspect runtime availability |
| `duckflight_pg_serve(address, users_file, management_token)` | `protocol`, `address` | Start PostgreSQL wire protocol |
| `duckflight_adbc_serve(address, users_file, management_token)` | `protocol`, `address` | Start Arrow Flight SQL |
| `duckflight_servers(management_token)` | `protocol`, `address` | List active endpoints |
| `duckflight_stop(protocol, address, management_token)` | `status` | Stop an endpoint |

The Flight SQL endpoint is compatible with ADBC Flight SQL clients. It can also be reached through
DuckDB's Airport extension when Airport is configured against the Flight endpoint.

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

An authorized release build embeds the private core directly into the extension:

```sh
./scripts/build-bundled.sh /path/to/private/duckflight
```

See [docs/BUNDLED_CORE.md](docs/BUNDLED_CORE.md) for the local build and per-platform GitHub Release
asset model.

## Community Extension status

This repository is structured for DuckDB Community Extension CI and includes a provisional
`description.yml`. Before an upstream submission, its repository URL and `repo.ref` must identify a
real public Git commit. Whether a Community Extension may require a separately installed proprietary
runtime is not relevant to the install path: the intended artifact is one bundled extension. Before
central Community builds can produce it, each immutable platform core library must exist as a GitHub
Release asset and its exact URL and SHA-256 must be added to `core-assets.lock`. The mandated
`make release` entrypoint then fetches, verifies, and embeds it during the build.

## Security

The bundled core executes native code in the DuckDB process. Release inputs must be immutable and
checksum-verified. Keep `DUCKFLIGHT_MANAGEMENT_TOKEN` out of shared SQL history and logs, and bind
server endpoints to loopback unless they are intentionally protected and exposed. See
[SECURITY.md](SECURITY.md) for reporting guidance.

## License

The extension shim, ABI crate, and test mock in this repository are available under the MIT License.
The private core source license is independent. Production release assets must carry an explicit
binary license grant from the core copyright holder that is compatible with the extension's MIT
metadata; the core owner can dual-license the binary without publishing its private source.

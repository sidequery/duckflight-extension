# Contributing

Contributions to the public extension shim, stable ABI, tests, and documentation are welcome.

Before opening a pull request, run:

```sh
git submodule update --init --recursive
cargo fmt --all -- --check
cargo check --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
uv run test/test_duckflight_auth.py
make configure
make test_debug
```

Changes to the ABI must remain C-compatible: do not pass Rust-owned strings, collections, trait
objects, futures, or allocator-owned values across the boundary. Preserve existing ABI v1 fields and
semantics; incompatible changes require a new versioned symbol and API table.

Do not add private runtime source, runtime binaries, generated shared libraries, build directories,
credentials, or `.duckdb_extension` artifacts to the source tree. Proprietary platform core
libraries belong only in versioned release assets consumed by the bundled release build.
`core-assets.lock` may contain only immutable release URLs and verified SHA-256 values.

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
DuckDB 2.0 at final pin `d54cbbe3df`, initially labeled `v2.0.0-alpha44357`.
The extension uses the stable v1 C API (`v1.5.6`); that metadata version identifies the API,
while the bundled native core must match the DuckDB host release.
Generated shared libraries, build trees, and `.duckdb_extension` artifacts are ignored
and must not be committed.

The initial Community release supports Linux and macOS on amd64 and arm64. Windows, WebAssembly,
and Linux musl are explicitly excluded until matching bundled core payloads pass the same release
smoke test; unsupported platforms never fall back to a source checkout or runtime download.

An authorized release build embeds the private core directly into the extension:

```sh
./scripts/build-bundled.sh /path/to/private/duckflight
```

See [docs/BUNDLED_CORE.md](docs/BUNDLED_CORE.md) for the local build and per-platform GitHub Release
asset model. The current payloads and their checksums are pinned in `core-assets.lock`.

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

## Protocol and container tests

Run the real-host protocol smoke test against a freshly bundled extension:

```sh
uv run test/release/http_quack_smoke.py /path/to/duckdb build/release/duckflight.duckdb_extension
```

To verify a signed installation, including HTTP and Quack, keep unsigned extensions disabled:

```sh
uv run test/release/http_quack_smoke.py /path/to/duckdb --repository https://extensions.sidequery.dev
```

The Docker workflow builds and smoke-tests Linux amd64 and arm64 natively on pull requests.
On `main` (or a manual run against `main`) it publishes tested images to
`ghcr.io/sidequery/duckflight-extension`, with `latest` and full commit-SHA tags. Prefer a
commit tag or image digest for deployments. The publishing workflow checks that both
architectures are available without registry credentials. Replace
`duckflight:local` in the commands above with that registry image and tag to consume a
published build.

To run the same real-client container checks locally:

```sh
uv run test/docker_smoke.py duckflight:local
```

The seven native server-control functions are documented by hand in
`description.yml` and `docs/SQL_FUNCTIONS.md` because the v1 C API does not expose
description setters for C API table functions. The compatibility macros carry catalog
comments, which the Community Extensions page collects automatically.

## Documentation tests

Code blocks in the documentation tagged `doctest` run against a real host:

```sh
uv run test/docs_snippets.py /path/to/duckdb build/release/duckflight.duckdb_extension
```

Omit the extension path to install the published Community build instead. The same
script checks relative links, heading anchors, and that the helper URL in the docs is
pinned to the version in `Cargo.toml`; run only those checks with `--links-only`.
Tag a new block by adding `doctest` (Python, shell), `doctest=host` or `doctest=pg`
(SQL) after its language. The script's docstring describes how blocks run.

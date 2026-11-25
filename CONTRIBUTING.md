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

## Documentation tests

Code blocks in the documentation tagged `doctest` run against a real host:

```sh
uv run test/docs_snippets.py /path/to/duckdb /path/to/duckflight.duckdb_extension
```

Omit the extension path to install the published Community build instead. The same
script checks relative links, heading anchors, and that the helper URL in the docs is
pinned to the version in `Cargo.toml`; run only those checks with `--links-only`.
Tag a new block by adding `doctest` (Python, shell), or `doctest=host`, `doctest=pg` or
`doctest=duckdb` (SQL) after its language. The script's docstring describes how blocks run.

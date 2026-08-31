# Contributing

Contributions to the public extension shim, stable ABI, tests, and documentation are welcome.

Before opening a pull request, run:

```sh
git submodule update --init --recursive
cargo fmt --all -- --check
cargo check --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
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

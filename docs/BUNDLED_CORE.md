# Bundled core build

The release artifact is one platform-specific `duckflight.duckdb_extension`. It embeds the
DuckFlight core; users do not install or download a sidecar library.

The public repository can build without the private core for source review, formatting, linting,
function discovery, and mock lifecycle tests. A release-producing build supplies a platform core
library through `DUCKFLIGHT_CORE_BUNDLE_PATH`. `build.rs` embeds those bytes in the extension. At
`LOAD`, the extension writes the core to a process-private temporary file, loads it, and removes it
after the library is unloaded. A bundled build ignores `DUCKFLIGHT_CORE_PATH`.

To build from an authorized private DuckFlight checkout:

```sh
./scripts/build-bundled.sh /path/to/private/duckflight
```

The script builds `duckflight-core-ffi` with its native dependencies closed for the target, embeds it
in the extension, adds DuckDB's extension metadata, and leaves the final artifact at
`build/release/duckflight.duckdb_extension`. Generated archives and extension binaries are ignored
and must never be committed.

For DuckDB Community's central per-platform build, publish the proprietary core libraries as
versioned GitHub Release assets, with a pinned SHA-256 for every supported platform. The Community
build must download the exact platform library before Cargo runs, verify it, and set
`DUCKFLIGHT_CORE_BUNDLE_PATH` to that file for the build only. The library bytes become part of the
single `.duckdb_extension`; there is no runtime download or sidecar installation. Do not use a
`latest` URL. Until those immutable release assets and checksums exist, the public CI build remains
the source-review/mock build and is not a production Community artifact.

The repository includes an intentionally empty `core-assets.lock`. Once its platform rows contain
real immutable URLs and SHA-256 values, the standard `make release` invoked by DuckDB's reusable CI
downloads and verifies the matching asset before Cargo runs. The downloaded library stays under the
ignored `build/` tree and is embedded into the final extension; it is never committed or installed
on the user's machine as a sidecar.

Every core asset must include an explicit binary-distribution license from the core copyright
holder that is compatible with the extension's MIT metadata. The core owner may dual-license that
binary without publishing the private source, but a source checkout's default license is not a
substitute for the separate release grant.

The distribution entrypoint is pinned to an exact `extension-ci-tools` commit. That upstream
reusable workflow currently calls some transitively tag- or branch-pinned actions. Treat this as
upstream build-system trust and re-audit the pinned workflow before producing release artifacts;
the public quality workflow's direct actions are commit-pinned.

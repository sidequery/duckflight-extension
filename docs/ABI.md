# DuckFlight core ABI

The production extension embeds the platform core library in the same platform-specific extension
artifact and discovers `duckflight_core_api_v1` after loading those embedded bytes. Public-source
development builds can discover the same symbol in the library named by `DUCKFLIGHT_CORE_PATH`.
In both modes the function returns a static `DuckflightCoreApiV1` table.

ABI v1 uses only fixed-layout C representations:

- numeric status and protocol values;
- borrowed pointer-and-length UTF-8 values;
- caller-owned output buffers;
- callbacks that are valid only for the duration of a call;
- an opaque runtime handle created and destroyed by the provider;
- DuckDB extension-info and access pointers borrowed only during `create`.

No Rust-owned value or allocator ownership crosses the boundary. A dynamic development build keeps
the provider library loaded until after the opaque handle is destroyed. ABI v1 providers must
synchronize mutable state because DuckDB can invoke table functions concurrently.

The extension checks the API version, structure size, complete callback table, create status, and
non-null handle before accepting a provider. Runtime failures use a caller-owned diagnostic buffer.
The canonical declarations live in `crates/duckflight-extension-abi/src/lib.rs`.

Adding fields to v1 is not supported because a current consumer requires the complete v1 structure.
An incompatible change must introduce a new symbol such as `duckflight_core_api_v2` and retain v1 as
long as released extensions depend on it.

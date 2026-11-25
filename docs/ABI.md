# DuckFlight core ABI

The production extension embeds the platform core library in the same platform-specific extension
artifact and discovers `duckflight_core_api_v1` after loading those embedded bytes. Public-source
development builds can discover the same symbol in the library named by `DUCKFLIGHT_CORE_PATH`.
In both modes the function returns a static `DuckflightCoreApiV1` table.

ABI v1 uses only fixed-layout C representations:

- numeric status and protocol values;
- borrowed pointer-and-length UTF-8 values;
- caller-owned output buffers;
- per-call callbacks and an optional host-lifetime ACL installer;
- an opaque runtime handle created and destroyed by the provider;
- DuckDB extension-info and access pointers borrowed only during `create`.

No Rust-owned value or allocator ownership crosses the boundary. A dynamic development build keeps
the provider library loaded until after the opaque handle is destroyed. ABI v1 providers must
synchronize mutable state because DuckDB can invoke table functions concurrently.

The extension checks the API version, structure size, complete callback table, create status, and
non-null handle before accepting a provider. Runtime failures use a caller-owned diagnostic buffer.
The canonical declarations live in `crates/duckflight-extension-abi/src/lib.rs`.

The v1 function table and the original create-options prefix have fixed layouts.
A shim with built-in ACLs passes `DuckflightCoreAclOptionsV1` to `create`. Its
first field is the original options structure, with `struct_size` set to the
size of the enclosing structure. Older providers read the prefix. New providers
read the installer pointer only when the size covers it. The installer takes a
live host database handle and remains callable for the host extension's lifetime.
The core checks the exact host version before calling it.

An incompatible change must introduce a new symbol such as `duckflight_core_api_v2` and retain v1 as
long as released extensions depend on it.

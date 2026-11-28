# Install

DuckFlight is distributed as one self-contained extension file per platform. Nothing is downloaded
at load time and there is no sidecar library.

## Which build you get

DuckDB picks the build matching its own version and platform. The extension must match the DuckDB
release exactly, so the DuckFlight version you receive depends on the DuckDB you run.

| Your DuckDB | Channel | DuckFlight | Signed by | Protocols |
| --- | --- | --- | --- | --- |
| 2.0.1 | [Community](#duckdb-community-extensions) | 0.1.21 | DuckDB | PostgreSQL, Flight SQL, HTTP, Quack |
| 2.0.1 | [Sidequery repository](#sidequery-repository) | 0.2.1 | Sidequery | PostgreSQL, Flight SQL, HTTP, Quack |
| 2.0.1 | [GitHub release artifact](#github-release-artifacts) | 0.2.1 | Unsigned | PostgreSQL, Flight SQL, HTTP, Quack |
| 2.0.0 | Sidequery repository | 0.2.1 | Sidequery | PostgreSQL, Flight SQL, HTTP, Quack |
| 2.0.0 | GitHub release artifact | 0.2.1 | Unsigned | PostgreSQL, Flight SQL, HTTP, Quack |
| 2.0.0 | Community | 0.1.20 | DuckDB | PostgreSQL, Flight SQL, HTTP, Quack |
| 1.5.6 | Community | 0.1.12 | DuckDB | PostgreSQL, Flight SQL |
| 1.5.6 | [Docker image](DOCKER.md) | Built from source | Unsigned | PostgreSQL, Flight SQL |

From 0.1.21, one file per platform serves both DuckDB 2.0.0 and 2.0.1.
[SQL access controls](ACCESS_CONTROL.md) need DuckDB 2.0.1.

The [account store](AUTHENTICATION.md#the-account-store), which the quickstart and guides use for
users and tokens, needs DuckFlight 0.2.0 or later. Earlier builds, including 0.1.12 for DuckDB
1.5.6 and the Docker image, read users and tokens from
[`duckflight.toml`](CONFIGURATION.md#legacy-users-and-tokens) instead.

Supported platforms are Linux and macOS on amd64 and arm64 (`linux_amd64`, `linux_arm64`,
`osx_amd64`, `osx_arm64`). Windows, WebAssembly and Linux musl are not supported.

Community serves 0.1.21 for DuckDB 2.0.1 until it publishes 0.2.1, and 0.1.20 for DuckDB 2.0.0. For
0.2.1, install from the [Sidequery repository](#sidequery-repository).

If you installed from Community on DuckDB 2.0.1 before 0.1.21 was published, you have 0.1.17,
which loads but reports `DuckFlight host factory requires DuckDB v2.0.0; host is v2.0.1` in
`duckflight_core_status()`. Run `update extensions (duckflight);` and restart DuckDB.

## Check your installation

```sql doctest=host
pragma version;

select extension_version, installed_from
from duckdb_extensions()
where extension_name = 'duckflight';

select * from duckflight_core_status();
```

`loaded = true` means the core initialized and listeners can be started. It does not mean a
listener is running; use `duckflight_servers()` for that. `abi_version` is the interface version
between the extension and its core, not the DuckDB or DuckFlight release version. When `loaded` is
false, `detail` says why.

## DuckDB Community Extensions

```sql
install duckflight from community;
load duckflight;
```

This is the recommended channel on DuckDB 2.0.1 and 1.5.6. To pick up a newer release later, run
`update extensions (duckflight);` and restart DuckDB.

## Sidequery repository

Sidequery publishes signed DuckDB 2.0.0 and 2.0.1 builds at `https://extensions.sidequery.dev`. Enable custom
repositories and register it:

```sql
set allow_extension_repositories = 'allowed';
create extension repository sidequery
    with prefix 'https://extensions.sidequery.dev';

install duckflight from sidequery;
load duckflight from sidequery;
```

DuckDB installs `httpfs` automatically for HTTPS repository access. Keep unsigned extensions
disabled; these builds are verified against Sidequery's repository signing key. To inspect the key
fingerprint DuckDB recorded:

```sql
select repository_name, prefix, key_fingerprints
from duckdb_extension_repositories()
where repository_name = 'sidequery';
```

Sidequery's fingerprint is:

```text
sha256:3913381c7400db553886bcf98a0f5dddaed632a3502e8cf618d40ecabc4ee24d
```

## GitHub release artifacts

Use this only when you cannot reach an extension repository. Release artifacts are not signed, so
DuckDB must be started with unsigned extensions enabled, which weakens extension-signature
protection for that process.

1. Download the asset for your platform from the
   [latest release](https://github.com/sidequery/duckflight-extension/releases/latest). Assets are
   named `duckflight-<platform>.duckdb_extension` and run on DuckDB 2.0.0 and 2.0.1.
2. Verify the download against the release's `SHA256SUMS`.
3. Rename the file to `duckflight.duckdb_extension`.
4. Start DuckDB with `duckdb -unsigned` and load the file by absolute path:

```sql
load '/absolute/path/to/duckflight.duckdb_extension';
select * from duckflight_core_status();
```

## Docker and Kubernetes

The container image packages the DuckDB CLI, the extension, and a launcher that starts PostgreSQL
and Flight SQL listeners against a persistent database. See [Docker](DOCKER.md). For Iceberg or
DuckLake workers on Kubernetes, use the [Helm chart](../charts/duckflight/README.md).

## Next

Create the first administrator and start a listener by following the
[quickstart](../README.md#quickstart), then connect a [client](CLIENTS.md).

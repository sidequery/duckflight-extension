# DuckFlight DuckDB extension

DuckFlight turns any DuckDB database into a PostgreSQL and Arrow Flight SQL server. Query it with
`psql`, PostgreSQL drivers and BI tools, ADBC Flight SQL clients, or another DuckDB instance through
Airport. Every client reads and writes the same live database—there is no second service to run and
no data to export or copy.

- [Install and load](#install-and-load)
- [SQL API](#sql-api)
- [Authentication setup](#authentication-setup)
- [Build and test](#build-and-test)
- [Security](#security)
- [License](#license)

## Install and load

Install the signed extension directly from DuckDB Community Extensions:

```sql
install duckflight from community;
load duckflight;
```

DuckDB downloads the build matching your DuckDB version and platform. DuckFlight currently supports
Linux and macOS on amd64 and arm64.

<details>
<summary>Load an unsigned artifact from GitHub Releases</summary>

Download the artifact for your platform from the
[latest release](https://github.com/sidequery/duckflight-extension/releases/latest). Release
artifacts are not signed by DuckDB, so start the CLI with unsigned extensions enabled:

```sh
duckdb -unsigned
```

Then load the downloaded file by its absolute path:

```sql
load '/absolute/path/to/duckflight-v1.5.5-osx_arm64.duckdb_extension';
select * from duckflight_core_status();
```

Choose the asset matching `linux_amd64`, `linux_arm64`, `osx_amd64`, or `osx_arm64`. The
`-unsigned` flag weakens DuckDB's extension-signature protection for that process, so use it only
with an artifact downloaded from this repository's releases and verify its checksum when moving it
through another system.

</details>

## SQL API

DuckFlight adds server-control table functions and, when the real core initializes, PostgreSQL
compatibility macros and catalog views. This guide covers the basics useful to extension users.
The public test mock only implements server lifecycle operations; it does not provide the core's
SQL compatibility layer.

### Start, inspect, and stop servers

Run these in the DuckDB instance that loads the extension. Both protocols use that same database,
including an in-memory database. Keep the host process running while clients connect.

```sql
load duckflight;

select * from duckflight_core_status();
select * from duckflight_pg_serve('127.0.0.1:5433', '/path/to/duckflight.toml');
select * from duckflight_flight_serve('127.0.0.1:31337', '/path/to/duckflight.toml');
select * from duckflight_servers();

select * from duckflight_stop('pgwire', '127.0.0.1:5433');
select * from duckflight_stop('flight', '127.0.0.1:31337');
```

| Function | Arguments | Returned columns |
| --- | --- | --- |
| `duckflight_core_status()` | None | `loaded` (boolean), `abi_version` (unsigned bigint), `detail` (varchar) |
| `duckflight_pg_serve(address, config_file)` | Two varchar values | `protocol`, `address` (varchar) |
| `duckflight_flight_serve(address, config_file)` | Two varchar values | `protocol`, `address` (varchar) |
| `duckflight_servers()` | None | `protocol`, `address` (varchar), one row per listener |
| `duckflight_stop(protocol, address)` | Two varchar values | `status` (varchar) |

Start functions return after starting a background listener. Use `host:port` for the address;
port `0` asks the operating system to choose a free port. The returned address is authoritative:
use it when connecting or stopping, including when a hostname resolves to an IP address.

`duckflight_stop` accepts `pgwire` (also `postgres` or `postgresql`) and `flight`. Stopping an
address that is not registered returns a `no ... server on ...` status. The public extension's
Flight function is `duckflight_flight_serve`; `duckflight_adbc_serve` and the `adbc` protocol name
from older core examples are not supported here. ADBC clients connect to the Flight SQL listener.

`loaded = true` reports core initialization, not whether a listener is running. Use
`duckflight_servers()` to list listeners. `abi_version` is the extension/core interface version,
not the DuckDB or DuckFlight release version.

Both listeners require an authentication config, and non-loopback listeners require TLS. See
[Authentication setup](#authentication-setup) for config creation and client connection examples.

### Common PostgreSQL compatibility helpers

The core bootstraps SQL macros in the host database for PostgreSQL-style queries and client
compatibility. These are representative helpers, not an exhaustive PostgreSQL function catalog.
Some names also have native DuckDB overloads; inspect the installed signatures when in doubt.

| Area | Useful signatures | Purpose |
| --- | --- | --- |
| Strings | `initcap(s)`, `btrim(s, chars)` | Capitalize space-separated words; trim specified characters |
| SQL quoting | `quote_literal(s)`, `quote_ident(s)`, `quote_nullable(s)` | Quote SQL values or identifiers; represent a null as SQL text |
| Numbers | `div(a, b)`, `width_bucket(x, minv, maxv, count)` | Truncated division; equal-width numeric buckets |
| Aggregates | `every(x)` | Boolean AND across rows |
| Arrays | `array_ndims(arr)`, `array_remove(arr, val)`, `array_replace(arr, old, new)` | Inspect dimensions or transform array elements |
| JSON | `json_typeof(j)`, `jsonb_array_length(j)` | Inspect a JSON value or array length |
| JSON aggregates | `json_agg(x)`, `json_object_agg(k, v)` | Build JSON arrays or objects from rows |
| Date/time | `to_char(val, fmt)`, `to_date(val, fmt)`, `to_timestamp(val, fmt)` | Format or parse supported date/time patterns |

For example, after loading a bundled extension:

```sql
select initcap('hello world');                          -- Hello World
select btrim('..hello..', '.');                         -- hello
select quote_ident('order');                           -- "order"
select div(7, 2);                                      -- 3
select array_remove([1, 2, 1], 1);                      -- [2]
select json_typeof('{"ok":true}');                      -- object
select to_char(date '2026-09-06', 'YYYY-MM-DD');          -- 2026-09-06
```

These helpers implement a subset of PostgreSQL behavior. For example, `btrim` here takes two
arguments. `to_char` explicitly handles `YYYY-MM-DD`, `Day`, `YYYY-MM-DD HH24:MI:SS`, and
`9999.99`; other patterns fall back to converting the value to text. `to_date` translates
`YYYY-MM-DD`, and the two-argument `to_timestamp` translates `YYYY-MM-DD HH24:MI:SS`; other
patterns are passed to DuckDB's `strptime`. Do not assume arbitrary PostgreSQL format patterns,
array null semantics, or all PostgreSQL overloads are supported.

### Catalogs and client SQL

The core also supplies compatibility metadata for PostgreSQL clients to discover tables,
columns, and types. PgWire applies SQL compatibility rewrites on top of those objects.
That rewriting is part of the PostgreSQL protocol path: running SQL directly in the host DuckDB
does not pass through it. Flight SQL exposes metadata through its own protocol methods.

### DuckFlight session and diagnostic utilities

The core also adds the following `duckflight_*` macros. Scalar macros go in the select list;
table macros are queried with `select * from ...`.

| Function | Result | Purpose in a PgWire session |
| --- | --- | --- |
| `duckflight_current_user()` | varchar | User associated with the client session |
| `duckflight_current_database()` | varchar | Client-facing database name, which can differ from the underlying DuckDB catalog |
| `duckflight_visible_database()` | varchar | Underlying DuckDB database selected for catalog visibility |
| `duckflight_session_pid()` | integer | PgWire backend/session identifier, not the host operating-system process ID |
| `duckflight_runtime_databases()` | Table with `datname` | Distinct database names represented in tracked PgWire sessions, not all attached databases |
| `duckflight_pg_stat_activity()` | Table with session and query metadata | Inspect tracked PgWire sessions, including `pid`, `datname`, `usename`, `application_name`, `state`, and `query` |
| `duckflight_pg_stat_ssl()` | Table with TLS metadata | Inspect `pid` and `ssl` for sessions belonging to the current user |
| `duckflight_database_oid(name)` | unsigned integer | Generate the database identifier used by the PostgreSQL compatibility catalogs |

Run these diagnostics through a PostgreSQL client connected to DuckFlight:

```sql
select duckflight_current_user(),
       duckflight_current_database(),
       duckflight_visible_database(),
       duckflight_session_pid();

select * from duckflight_runtime_databases();

select pid, datname, usename, application_name, state, query
from duckflight_pg_stat_activity();

select pid, ssl from duckflight_pg_stat_ssl();

select duckflight_database_oid(duckflight_current_database());
```

PgWire installs connection-local identity values and runtime snapshots for these helpers.
They describe PgWire sessions, not Flight SQL clients or every connection in the host process.
For activity rows belonging to other users, query text is `<insufficient privilege>` and details
such as state, client address, and timestamps are null. The SSL table includes only the current
user's sessions; TLS version, cipher, and certificate-detail columns are currently null.

Direct calls in the loading DuckDB connection do not receive that PgWire session context. The
bootstrap defaults are `duckflight` for the current user and current database, `memory` for the
visible database, `0` for the session PID, and empty tables for runtime databases, activity, and
SSL. These are placeholders, not measurements of the host connection. Do not treat them as
Flight SQL session diagnostics either.

`duckflight_database_oid(name)` is a catalog compatibility helper, not a persistent application
identifier: it assigns `1`, `2`, and `3` to `postgres`, `template0`, and `template1`, respectively,
and derives other values from DuckDB's hash of the name. Do not rely on those derived values as
collision-free identifiers or stable IDs across DuckDB versions.

Other core utilities can belong to a different runtime. For example, `sidequery_files()` is
registered for cloud-runner requests; loading this extension does not register it.

### Discover functions in your installed build

Use DuckDB's function catalog to see names, signatures, and macro definitions:

```sql
select schema_name, function_name, function_type, parameters, macro_definition
from duckdb_functions()
where starts_with(function_name, 'duckflight_')
   or function_name in ('initcap', 'btrim', 'array_remove', 'json_typeof', 'to_char')
order by schema_name, function_name, function_type;
```

This query also includes internal helpers and any matching functions you created yourself.
The installed core determines compatibility behavior; newer standalone-core documentation may
describe features absent from your extension build.

## Authentication setup

Both extension listeners take the path to one `duckflight.toml` file:

```sql
select * from duckflight_pg_serve('127.0.0.1:5433', '/run/secrets/duckflight.toml');
select * from duckflight_flight_serve('127.0.0.1:31337', '/run/secrets/duckflight.toml');
```

Existing users-only TOML files remain valid. The same file may additionally contain generated
Airport/direct bearer-token hashes and one TLS identity shared by PgWire and Flight.
For extension-managed listeners, this file is authoritative: process-wide authentication and
per-protocol TLS environment settings used by standalone DuckFlight services are not merged into or
allowed to override it.

### Secure defaults

Every listener requires at least one configured user, bearer token, or mTLS identity. An empty
configuration does not start an anonymous server.

Plaintext transport is permitted only when the address actually bound by the operating system is a
loopback address (`127.0.0.0/8` or `::1`). Binding `0.0.0.0`, `::`, or any concrete non-loopback
address requires `[tls]`; startup fails before the listener thread starts when TLS is missing or
invalid. The certificate's subject alternative names must cover the hostname used by clients.

TLS protects server identity, credentials, queries, and results. The authentication protocols differ:

| Client | Authentication on the wire | Required config |
| --- | --- | --- |
| PostgreSQL | PostgreSQL SCRAM-SHA-256 | `[users.*]` |
| ADBC Flight SQL username/password | Basic authorization during the Flight handshake, then a server-issued bearer | `[users.*]`; TLS outside loopback |
| Airport or direct bearer ADBC | Bearer on every Flight RPC; no username/password handshake | `[tokens.*]`; TLS outside loopback |
| mTLS | Verified client certificate mapped by SHA-256 fingerprint | `[tls]` with `client_ca` and `identities` |

The Flight username/password exchange is deliberately the standard ADBC-compatible flow, not a
SCRAM exchange. Never use it over an untrusted plaintext transport. Server-issued Flight bearer
sessions have a built-in 24-hour maximum lifetime and are also invalidated when the listener stops
or the client closes its Flight SQL session. Standard ADBC clients reconnect and repeat the
username/password handshake after that maximum is reached.

### Use the helper

The dependency-free PEP 723 script performs atomic mode-`0600` updates and validates the complete
file:

```sh
uv run scripts/duckflight_auth.py user add alice --file duckflight.toml
uv run scripts/duckflight_auth.py user list --file duckflight.toml
uv run scripts/duckflight_auth.py user test alice --file duckflight.toml

uv run scripts/duckflight_auth.py token add airport --file duckflight.toml
uv run scripts/duckflight_auth.py token list --file duckflight.toml
uv run scripts/duckflight_auth.py token test airport --file duckflight.toml

uv run scripts/duckflight_auth.py check --file duckflight.toml
uv run scripts/duckflight_auth.py tls --file duckflight.toml
```

Password and token verification prompts do not put credentials in process arguments. `token add`
prints the new raw token once as `token=...`; copy it directly into the client secret manager. Only
its SHA-256 digest is written to `duckflight.toml`. `--replace` rotates an existing user or token.
Generated tokens grant `query:execute` and `transaction:manage` by default. Airport and ADBC create
a Flight SQL transaction even for reads, so both scopes are required for their least-privilege read
path. This still denies query mutation, ingestion, and administrative SQL. Grant additional
permissions with repeated `--scope`, or use the explicit `--full-access` switch when the client
genuinely needs the complete set.

### Configuration schema

A complete configuration can contain all three sections:

```toml
[users.alice]
password_hash = "<64 lowercase hexadecimal characters>"
salt = [<16 decimal byte values from 0 through 255>]
iterations = 10000

[tokens.airport]
sha256 = "<64 lowercase hexadecimal characters>"
subject = "airport"
scopes = [
  "query:execute",
  "transaction:manage",
]

[tls]
cert = "server.crt"
key = "server.key"
```

Relative certificate paths are resolved relative to `duckflight.toml`, not the process working
directory. The same certificate and private key are used for both PgWire and Flight; the protocols
negotiate independently over their respective ports.

The angle-bracket values above describe the schema and are not usable credentials. Quote names that
are not valid bare TOML keys.

#### Users

`password_hash` is the 32-byte value produced by
`PBKDF2-HMAC-SHA256(password, salt, iterations, output_length=32)`. Store that derived value, never a
plaintext password. All users in one file must use the same iteration count. DuckFlight requires at
least 4,096 iterations and the helper generates 10,000 by default.

Possession of the verifier and salt permits offline password guessing. Prefer strong unique
passwords even though plaintext passwords are never stored.

#### Bearer tokens

Each token entry stores a SHA-256 digest, an audit subject, and authorization scopes. Tokens
generated by the helper contain 256 bits of randomness, so a direct digest is appropriate; do not
put a human-chosen password in a token entry. The raw token belongs only in the client secret
manager.

Airport sends a preconfigured bearer and does not perform the ADBC username/password handshake. To
connect Airport, generate a token, start the Flight listener, then store the raw value with the
Airport secret type:

```sql
create secret duckflight_airport (
  type airport,
  scope 'grpc+tls://flight.example.com:31337',
  auth_token 'the-token-printed-by-token-add'
);

attach 'grpc+tls://flight.example.com:31337' as remote (type airport);
select * from remote.public.example limit 10;
```

Use a temporary DuckDB secret or a suitably protected persistent secret store. Do not inline the
token in `attach`, application logs, or checked-in SQL. Airport's current authentication interface is
documented by [Query.Farm](https://query.farm/products/extensions/airport/).

#### Optional mTLS

mTLS is an advanced alternative for deployments that manage client certificates:

```toml
[tls]
cert = "server.crt"
key = "server.key"
client_ca = "client-ca.crt"
client_cert_mode = "required"

[tls.identities."sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"]
subject = "reporting-service"
scopes = ["query:execute"]
```

When `client_ca` is configured, identity mappings are mandatory and client certificates are required
by default. Set `client_cert_mode = "optional"` only for an intentional mixed deployment where
other clients authenticate using passwords or bearer tokens. A request presenting both a client
certificate and bearer token is rejected as ambiguous.

### Treat the file as credential material

- Never commit it, attach it to a GitHub Release, include it in CI artifacts, or bake it into an
  image. This repository ignores `/duckflight.toml` and the legacy `/users.toml` name.
- Store production copies in a secret manager and provision them only at deployment time. Keep
  development, staging, and production files separate.
- Limit ownership to the account running DuckDB. Use mode `0600` on Unix and an equivalent
  account-only ACL on Windows. Protect parent directories, backups, certificate private keys, and
  the client secret store too.
- Mount the deployed copy read-only where possible. Run the helper against a writable administrative
  copy, validate it, then publish a new secret-manager version.
- Restart the affected listener after changing the file; listeners load a consistent snapshot at
  startup rather than watching for partial changes.
- Do not print the file, password hashes, salts, token hashes, raw tokens, or plaintext passwords in
  logs, tickets, terminal recordings, or support bundles.

If the file is exposed, rotate every password verifier with new salts and passwords and rotate every
bearer token. If only a raw bearer token is exposed, replace that token entry and update its client
secret. Stop/restart the listener to invalidate locally issued Flight sessions immediately.

Starting, listing, and stopping listeners requires ordinary SQL access to the loaded extension. On a
host that accepts untrusted SQL, isolate each tenant in a separate DuckDB process and control whether
the extension is installed or loaded. Listener authentication is not a substitute for process
isolation or SQL authorization inside the host DuckDB process.

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
[`core-v0.1.4` release](https://github.com/sidequery/duckflight-extension/releases/tag/core-v0.1.4)
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

## Security

The bundled core executes native code in the DuckDB process. Release inputs must be immutable and
checksum-verified. SQL callers are trusted to manage DuckFlight listeners, just as they are trusted
to operate the DuckDB instance. The core rejects non-loopback listeners without TLS and rejects
listeners with no authentication method. See [SECURITY.md](SECURITY.md) for reporting guidance.

## License

This extension is licensed under the [MIT License](LICENSE). Sidequery also licenses the bundled
core binaries under MIT; the core source code remains private and is not covered by that license.

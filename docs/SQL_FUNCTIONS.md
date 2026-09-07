# SQL functions

DuckFlight adds server-control table functions and, when the real core initializes, PostgreSQL
compatibility macros and catalog views. This guide covers the basics useful to extension users.
The public test mock only implements server lifecycle operations; it does not provide the core's
SQL compatibility layer.

## Start, inspect, and stop servers

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
[Authentication](AUTHENTICATION.md) for config creation and client connection examples.

## Common PostgreSQL compatibility helpers

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

## Catalogs and client SQL

The core also supplies compatibility metadata for PostgreSQL clients to discover tables,
columns, and types. PgWire applies SQL compatibility rewrites on top of those objects.
That rewriting is part of the PostgreSQL protocol path: running SQL directly in the host DuckDB
does not pass through it. Flight SQL exposes metadata through its own protocol methods.

## DuckFlight session and diagnostic utilities

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

## Discover functions in your installed build

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

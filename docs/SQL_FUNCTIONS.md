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

Internal `duckflight_*` macros and catalog views support these paths, including session identity
and database visibility. They are implementation helpers rather than additional server-control
functions. Prefer ordinary DuckDB catalog functions locally and your client's metadata APIs
remotely; internal helper names do not imply the same session behavior in every connection.

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

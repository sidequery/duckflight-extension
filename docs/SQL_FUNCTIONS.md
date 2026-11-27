# SQL reference

Loading DuckFlight adds four groups of SQL objects to the host database:

- [Server functions](#server-functions) that start, list and stop listeners.
- [Setup functions](#setup-functions) that manage accounts in the account store, check a config
  file, or create credentials in `duckflight.toml` for builds that predate the store.
- [PostgreSQL compatibility helpers](#postgresql-compatibility-helpers) so PostgreSQL-flavoured
  queries and client tools work.
- [Session diagnostics](#session-diagnostics) that describe connected PostgreSQL clients.

## Server functions

Run these in the DuckDB process that loaded the extension. Every listener serves that process's
database, including an in-memory one, for as long as the process stays running.

Starting, stopping and listing listeners is reserved for that process, and so are the
[setup functions](#setup-functions). A client connected through a listener gets an error from
these functions whatever its credentials allow; `duckflight_core_status()` stays available to it.

A listener needs an account to authenticate, so this example creates the first administrator
before starting listeners:

```sql doctest=host
load duckflight;

select * from duckflight_core_status();
select * from duckflight_init('alice');
select * from duckflight_serve('postgres', '127.0.0.1:5433');
select * from duckflight_serve('flight', '127.0.0.1:31337');
select * from duckflight_serve('http', '127.0.0.1:4080');
select * from duckflight_serve('quack', '127.0.0.1:4081');
select * from duckflight_servers();

select * from duckflight_stop('postgres', '127.0.0.1:5433');
select * from duckflight_stop('flight', '127.0.0.1:31337');
select * from duckflight_stop('http', '127.0.0.1:4080');
select * from duckflight_stop('quack', '127.0.0.1:4081');
```

| Function | Arguments | Returned columns |
| --- | --- | --- |
| `duckflight_core_status()` | None | `loaded` (boolean), `abi_version` (unsigned bigint), `detail` (varchar) |
| `duckflight_serve(protocol, address, config_file := '')` | Two varchar values and an optional named varchar | `protocol`, `address` (varchar) |
| `duckflight_servers()` | None | `protocol`, `address` (varchar), one row per listener |
| `duckflight_stop(protocol, address)` | Two varchar values | `status` (varchar) |

### Starting

`address` is `host:port`. Port `0` asks the operating system for a free port. The returned address
is authoritative: use it to connect and to stop, including when a hostname resolved to an IP
address. `duckflight_serve` returns once its background listener is running.

`protocol` names the listener to start:

| Listener | Protocol name |
| --- | --- |
| PostgreSQL | `postgres` (also `postgresql` or `pgwire`) |
| Flight SQL | `flight` |
| HTTP | `http` |
| Quack | `quack` |

Results and status messages report the PostgreSQL listener as `pgwire`.

`config_file` is the path to [`duckflight.toml`](CONFIGURATION.md#duckflighttoml). It is
optional. Either way the listener authenticates users and tokens against the host's
[account store](#account-store), which needs a user first; see `duckflight_init`. The file adds
TLS, OIDC, timeouts and the other [listener settings](CONFIGURATION.md):

```sql
select * from duckflight_serve('postgres', '127.0.0.1:5433');
select * from duckflight_serve('flight', '127.0.0.1:31337', config_file := 'duckflight.toml');
```

Every listener requires authentication, and a non-loopback address also requires TLS; see
[Authentication](AUTHENTICATION.md). A listener starts once the account store has a user, or when
the file configures an OIDC provider, an mTLS identity, or
[legacy users and tokens](CONFIGURATION.md#legacy-users-and-tokens). The HTTP and Quack
listeners accept bearer tokens, OIDC and mTLS, but not passwords.

DuckDB 1.5.6 receives DuckFlight 0.1.12, which starts listeners with the older
`duckflight_pg_serve(address, config_file)` and `duckflight_flight_serve(address, config_file)`
instead.

### Stopping

`duckflight_stop` takes the same protocol name and the address `duckflight_serve` returned.

A successful stop returns `stopped <protocol> server on <address>`. Stopping an address that is not
registered returns a `no ... server on ...` status rather than an error. ADBC clients connect to
the Flight SQL listener; there is no separate `adbc` protocol.

How long a stopping listener waits for running queries is set by
[`shutdown_grace_secs`](CONFIGURATION.md#top-level). Closing DuckDB stops every listener.

## Setup functions

These manage accounts from the DuckDB process that will serve them, with nothing else to install.
Over a listener, an administrator uses [account SQL](AUTHENTICATION.md#manage-accounts-with-sql)
instead.

### Account store

The account store holds password users and bearer tokens, each user with the role `reader`,
`writer` or `admin`. Once it has a user it is the only source of password users and tokens, and
the `[users]` and `[tokens]` of a config file are ignored. Listeners look accounts up at every
login, so an account added while they run works at once.

`duckflight_init`, shown [above](#server-functions), creates the store and its first
administrator. Then:

```sql doctest=host
select * from duckflight_user_add('bob', role := 'writer');
select * from duckflight_token_add('bob', 'nightly');
select * from duckflight_accounts();
```

A password can come from a file instead of being generated, and a config file's legacy accounts
can be imported:

```sql
select * from duckflight_init('alice', password_file := 'alice.password');
select * from duckflight_user_add('carol', password_file := 'carol.password');
select * from duckflight_import('duckflight.toml');
```

| Function | Arguments | Returned columns |
| --- | --- | --- |
| `duckflight_init(admin)` | One varchar value; optional `config_file`, `password_file` (varchar) | `user`, `password` (varchar, null with `password_file`) |
| `duckflight_user_add(name)` | One varchar value; optional `role` (varchar, default `reader`), `config_file`, `password_file` (varchar) | `user`, `password` (varchar, null with `password_file`) |
| `duckflight_token_add(user, token_name)` | Two varchar values; optional `config_file` (varchar) | `user`, `token_name`, `token` (varchar) |
| `duckflight_accounts()` | None; optional `config_file` (varchar) | `user`, `role` (varchar), `has_password` (boolean), `tokens` (unsigned bigint), `tenant` (varchar, null without one), one row per user |
| `duckflight_import(config_file)` | One varchar value | `kind` (`user` or `token`), `name`, `account`, `role`, `note` (varchar), one row per imported entry |

`duckflight_init` creates the store and its first user, with the `admin` role, and fails once the
store has a user. `duckflight_user_add` and `duckflight_token_add` need an initialized store, and
the token's user must exist. DuckFlight generates each password and token and returns it once, so
it does not appear in the statement or in your DuckDB history; copy it straight into the client's
secret manager. With `password_file` the password is read from that file, without its final
newline, and not returned. A token takes its user's role.

`duckflight_import` copies the `[users]` and `[tokens]` of a config file into the store in one
all-or-nothing step and leaves the file unchanged. Each token joins the store user named by its
`subject`. Entries already in the store with the same credential are skipped, so running it again
changes nothing; `note` says what happened to each entry. Importing into an empty store needs an
administrator in the file: a `[users]` entry or a token with the `query:admin` scope. See
[Upgrading from 0.1.x](AUTHENTICATION.md#upgrading-from-01x) for the roles imported accounts get.

The store lives beside the host database: `<database>.duckflight/store.duckdb` for a database
file, or `.duckflight/store.duckdb` in the working directory for an in-memory one. A config file
can name another store with [`[store]`](CONFIGURATION.md#store), or with `acl.store`.
`config_file` is needed only then, to select that store; leave it out otherwise. A `config_file`
that resolves to a different store than running listeners use is refused. These functions read
the file only to find the store, except `duckflight_import`.

With a DuckFlight core that predates the account store, each of these functions fails with an
error saying the installed core does not support it.

### Config file

```sql
select * from duckflight_config_check('duckflight.toml');
```

| Function | Arguments | Returned columns |
| --- | --- | --- |
| `duckflight_config_check(config_file)` | One varchar value | `users`, `tokens`, `oidc_providers` (unsigned bigint), `tls`, `acl` (boolean), `store_path` (varchar), `store_exists`, `store_initialized` (boolean), `store_users`, `ignored_users`, `ignored_tokens` (unsigned bigint) |

`duckflight_config_check` validates a file the way a listener reads it when it starts and reports
what it defines. It does not open the certificate files. The `store_` columns describe the account
store the file resolves to: its path (for a DuckLake store, the catalog with any password
redacted), whether it exists, whether it has a user, and how many users it has. `ignored_users`
and `ignored_tokens` count the file's `[users]` and `[tokens]` entries that the initialized store
makes ineffective; import them with `duckflight_import`. With a DuckFlight core that predates the
account store these columns are null. The check needs a file: there is no form without one.

### Config file credentials

`duckflight_token_create` and `duckflight_user_create` write `[tokens]` and `[users]` entries
into [`duckflight.toml`](CONFIGURATION.md#legacy-users-and-tokens). They are for builds and files
from before the account store. They refuse once the account store that file resolves to is
initialized, because the store would ignore what they add; use `duckflight_user_add` and
`duckflight_token_add` instead.

```sql
select * from duckflight_token_create('duckflight.toml', 'airport');
select * from duckflight_token_create('duckflight.toml', 'loader',
                                      scopes := 'query:execute,query:mutate,transaction:manage');
select * from duckflight_user_create('duckflight.toml', 'alice');
select * from duckflight_user_create('duckflight.toml', 'bob', password_file := 'bob.password');
```

| Function | Arguments | Returned columns |
| --- | --- | --- |
| `duckflight_token_create(config_file, name)` | Two varchar values; optional `scopes` (comma-separated varchar) | `name`, `token` (varchar) |
| `duckflight_user_create(config_file, name)` | Two varchar values; optional `password_file` (varchar) | `name`, `password` (varchar, null with `password_file`) |

As with the account store functions, a secret is never an argument: DuckFlight generates the token
or password and returns it once, and the file stores only a SHA-256 digest or a salted password
verifier. Without `scopes` a token gets `query:execute` and `transaction:manage`. See
[Authorization scopes](AUTHENTICATION.md#authorization-scopes) for the full list.

The functions create the file when it does not exist and otherwise append to it, leaving
everything already there, comments included, unchanged. The result is validated before it replaces
the file, which is written with mode `0600`. A name that already exists is an error: remove its
table from the file to replace it. A running listener keeps the file credentials it started with,
so stop and start it to pick up a new one.

### Status

`duckflight_core_status()` reports whether DuckFlight's core initialized. `loaded = true` does not
mean a listener is running; `duckflight_servers()` lists those. `abi_version` is the interface
version between the extension and its core, not the DuckDB or DuckFlight release. When `loaded` is
false, `detail` carries the reason.

## PostgreSQL compatibility helpers

DuckFlight installs SQL macros in the host database so common PostgreSQL functions exist. They
implement a subset of PostgreSQL behaviour, not a complete function catalog. Some names also have
native DuckDB overloads.

| Area | Signatures | Purpose |
| --- | --- | --- |
| Strings | `initcap(s)`, `btrim(s, chars)` | Capitalize space-separated words; trim specified characters |
| SQL quoting | `quote_literal(s)`, `quote_ident(s)`, `quote_nullable(s)` | Quote SQL values or identifiers; represent a null as SQL text |
| Numbers | `div(a, b)`, `width_bucket(x, minv, maxv, count)` | Truncated division; equal-width numeric buckets |
| Aggregates | `every(x)` | Boolean AND across rows |
| Arrays | `array_ndims(arr)`, `array_remove(arr, val)`, `array_replace(arr, old, new)` | Inspect dimensions or transform array elements |
| JSON | `json_typeof(j)`, `jsonb_array_length(j)` | Inspect a JSON value or array length |
| JSON aggregates | `json_agg(x)`, `json_object_agg(k, v)` | Build JSON arrays or objects from rows |
| Date/time | `to_char(val, fmt)`, `to_date(val, fmt)`, `to_timestamp(val, fmt)` | Format or parse supported date/time patterns |

```sql doctest=host
select initcap('hello world');                          -- Hello World
select btrim('..hello..', '.');                         -- hello
select quote_ident('order');                           -- "order"
select div(7, 2);                                      -- 3
select array_remove([1, 2, 1], 1);                      -- [2]
select json_typeof('{"ok":true}');                      -- object
select to_char(date '2026-09-06', 'YYYY-MM-DD');          -- 2026-09-06
```

### Known limits

| Helper | Limit |
| --- | --- |
| `btrim` | Takes two arguments only |
| `to_char` | Handles `YYYY-MM-DD`, `Day`, `YYYY-MM-DD HH24:MI:SS` and `9999.99`; any other pattern converts the value to text |
| `to_date` | Translates `YYYY-MM-DD`; other patterns are passed to DuckDB's `strptime` |
| `to_timestamp(val, fmt)` | Translates `YYYY-MM-DD HH24:MI:SS`; other patterns are passed to DuckDB's `strptime` |

Do not assume arbitrary PostgreSQL format patterns, PostgreSQL array null semantics, or every
PostgreSQL overload.

### Catalogs and query rewriting

DuckFlight also supplies PostgreSQL catalog views so clients can discover tables, columns and
types. On top of those, the PostgreSQL listener rewrites incoming SQL for compatibility. That
rewriting belongs to the PostgreSQL protocol path only: SQL run directly in the host DuckDB, or
over Flight SQL, HTTP or Quack, does not pass through it. Flight SQL exposes metadata through its
own protocol methods.

### List what your build installed

Each helper carries a catalog comment:

```sql doctest=host
select function_name, comment
from duckdb_functions()
where comment like 'PostgreSQL compatibility:%' or comment like 'Internal %'
order by function_name;
```

For names, signatures and macro definitions:

```sql doctest=host
select schema_name, function_name, function_type, parameters, macro_definition
from duckdb_functions()
where starts_with(function_name, 'duckflight_')
   or function_name in ('initcap', 'btrim', 'array_remove', 'json_typeof', 'to_char')
order by schema_name, function_name, function_type;
```

This also returns internal helpers and any matching functions you created yourself. What is listed
depends on the DuckFlight version installed.

## Session diagnostics

These macros describe PostgreSQL sessions. Run them through a PostgreSQL client connected to
DuckFlight. Scalar macros go in the select list; table macros are queried with `select * from`.

| Function | Result | Meaning |
| --- | --- | --- |
| `duckflight_current_user()` | varchar | User of this session |
| `duckflight_current_database()` | varchar | Database name the client connected to, which can differ from the underlying DuckDB catalog |
| `duckflight_visible_database()` | varchar | Underlying DuckDB catalog selected for catalog visibility |
| `duckflight_session_pid()` | integer | Session identifier. Not the operating-system process ID |
| `duckflight_runtime_databases()` | table with `datname` | Distinct database names across tracked PostgreSQL sessions, not every attached database |
| `duckflight_pg_stat_activity()` | table | Tracked sessions: `pid`, `datname`, `usename`, `application_name`, `state`, `query` and more |
| `duckflight_pg_stat_ssl()` | table | `pid` and `ssl` for the current user's sessions |
| `duckflight_database_oid(name)` | unsigned integer | Database identifier used by the compatibility catalogs |

```sql doctest=pg
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

### What to expect

- **Other users' sessions are redacted.** In `duckflight_pg_stat_activity()`, rows belonging to
  other users show `<insufficient privilege>` as the query, and their state, client address and
  timestamps are null.
- **SSL details are partial.** `duckflight_pg_stat_ssl()` lists only the current user's sessions.
  TLS version, cipher and certificate columns are currently null.
- **PostgreSQL sessions only.** These do not describe Flight SQL, HTTP or Quack clients.
- **Placeholders in the host session.** Called directly in the DuckDB process that loaded the
  extension, the macros return fixed defaults: `duckflight` for the current user and database,
  `memory` for the visible database, `0` for the session PID, and empty tables. These are not
  measurements of the host connection.
- **OIDs are not stable identifiers.** `duckflight_database_oid` assigns `1`, `2` and `3` to
  `postgres`, `template0` and `template1`, and derives every other value from DuckDB's hash of the
  name. Do not treat derived values as collision-free or stable across DuckDB versions.

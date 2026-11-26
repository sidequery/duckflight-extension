# Troubleshooting

Start with these two queries in the DuckDB process that loaded the extension. Most problems show
up in one of them.

```sql
select * from duckflight_core_status();
select * from duckflight_servers();
```

## Loading

### `loaded` is false: `DuckFlight host factory requires DuckDB v2.0.0; host is v2.0.1`

The extension loaded, but this build's core only runs on DuckDB 2.0.0. It is DuckFlight 0.1.17,
which Community served for DuckDB 2.0.1 before 0.1.21. Run `update extensions (duckflight);`
and restart DuckDB to get 0.1.21, which runs on both.

### `Failed to install remote extension "duckflight"`

The repository has no build for your DuckDB version and platform. The Sidequery repository carries
builds for DuckDB 2.0.0 and 2.0.1 only. Check `pragma version;` against the
[compatibility table](INSTALL.md#which-build-you-get).

### `duckflight_http_serve` or `duckflight_quack_serve` does not exist

You are on DuckDB 1.5.6, which receives DuckFlight 0.1.12 with the PostgreSQL and Flight SQL
listeners only. HTTP and Quack need DuckDB 2.0.0 or later.

## Starting a listener

| Error | Cause | Fix |
| --- | --- | --- |
| `Failed to read DuckFlight config: <path>` | The file is missing or cannot be read | Check the path, which is relative to the DuckDB process's working directory. Validate with `duckflight-auth check` |
| `Failed to parse DuckFlight config: <path>` | The file is not valid, or has a section this version does not support, such as `[acl]` on DuckFlight 0.1.20 | Validate with `duckflight-auth check` and remove unsupported sections |
| `DuckFlight extension listeners require at least one user, token, OIDC provider, or mTLS identity in the configuration file` | The config has no credentials | [Add a user or token](AUTHENTICATION.md#the-helper) |
| `DuckFlight refuses non-loopback listener 0.0.0.0:5433 without TLS` | The address is reachable from other machines and there is no `[tls]` section | Bind `127.0.0.1`, or [add a certificate](AUTHENTICATION.md#tls) |
| `DuckFlight http extension listeners require a token, OIDC provider, or mTLS identity; SCRAM users are not supported` | HTTP and Quack do not accept users and passwords | Add a [bearer token](AUTHENTICATION.md#bearer-tokens) |
| `Address already in use` | Another process, or an earlier listener, holds the port | Pick another port, or stop the old listener with `duckflight_stop` |
| `unsupported DuckFlight extension protocol: <name>` | `duckflight_stop` was given an unknown protocol | Use `pgwire`, `flight`, `http` or `quack` |

`duckflight_stop` returning `no pgwire server on <address>` means nothing is listening at exactly
that address. Copy the address from `duckflight_servers()`; a listener started on a hostname or on
port `0` is registered under the resolved address.

The helper reports `unexpected top-level field(s): pgwire` (or `acl`) for files containing those
sections, because it does not manage them. That message comes from the helper, not the listener.

## Connecting

### Connection refused

Nothing is listening on that address. The server lives inside the DuckDB process that called
`duckflight_*_serve`, so it stops when that process exits or its session is closed. Confirm with
`duckflight_servers()` that the listener is still registered, and wait for the start function to
return its row before connecting.

### `FATAL:  Password authentication failed for user ""`

The PostgreSQL password is wrong or the user is not in the config. The message can show an empty
user name even when one was sent. Remember that a listener reads the config when it starts: after `user add`, stop and
restart the listener.

### `FATAL:  TLS is required`

The PostgreSQL listener has a `[tls]` section and the client connected without TLS. Use
`sslmode=verify-full` with `sslrootcert` pointing at the issuing CA.

### `Invalid username or password` over Flight SQL

Same causes as the PostgreSQL password error. If you meant to use a bearer token, send it in the
`authorization` header instead of the `username` and `password` options; see
[Clients](CLIENTS.md#python-with-a-bearer-token).

### HTTP `401`

The `Authorization: Bearer` header is missing or the token is not in the config. HTTP never accepts
a user name and password.

### `Capability does not grant required scope: query:mutate`

The credential can read but not write. HTTP returns this with status `403`. Tokens default to
`query:execute` and `transaction:manage`; create one with the
[scopes](AUTHENTICATION.md#authorization-scopes) the client needs.

### SQLAlchemy: `syntax error at or near "SAVEPOINT"`

SQLAlchemy's `postgresql+psycopg` (version 3) dialect issues `SAVEPOINT` on connect, which DuckDB
does not support. Use `postgresql+psycopg2`. Psycopg 3 itself works when used directly.

### A PostgreSQL query naming another catalog fails with SQLSTATE `3D000`

PostgreSQL sessions cannot reference other databases by default. Enable
[`cross_catalog_reads`](CONFIGURATION.md#pgwire) to allow read-only queries across attached
catalogs.

### Quack: `Invalid DuckDB Quack RPC URI, needs to start with 'quack:'`

DuckDB's `quack` client takes `quack:host:port`, not an `http://` URL.

### Quack: `Invalid API key`

The token passed to `quack_query` or `attach` is not in the listener's config.

### Quack: `Quack session SQL is outside its fixed capability`

A write through a catalog attached with `type quack` used a token without `query:mutate`. On
DuckFlight 0.1.20 and earlier, attaching itself needs that scope, even for reads; from 0.1.21 a
read-only token can attach and query.

### Quack: `Unsupported protocol error for HTTP POST`

The client sent plain HTTP to a listener that has TLS. DuckDB's quack client defaults to plain
HTTP for `localhost` and to HTTPS for every other host. For a TLS listener on `localhost`, pass
`disable_ssl := false`, or `ssl_fingerprint` if the certificate is self-signed; see
[Clients](CLIENTS.md#quack-duckdb-to-duckdb).

### Quack: `invalid SQL` on `insert` into an attached catalog

On DuckFlight 0.1.20 and earlier, `insert into remote.main.t values (...)` through a catalog
attached with `type quack` fails. Upgrade to 0.1.21, or run the statement with `quack_query`.

### Airport will not load on DuckDB 2.0

The Airport build in DuckDB Community refuses to load on DuckDB 2.0 and reports that it is now
distributed from Query.Farm's extension repository. Install it as
[Airport's documentation](https://query.farm/products/extensions/airport/) describes.

## Session diagnostics return placeholders

`duckflight_current_user()` returning `duckflight` and `duckflight_pg_stat_activity()` returning no
rows means the query ran in the host DuckDB session rather than through a PostgreSQL client. See
[what to expect](SQL_FUNCTIONS.md#what-to-expect).

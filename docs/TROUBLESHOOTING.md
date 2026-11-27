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

### `duckflight_init is not supported by the installed DuckFlight core, which predates the account store`

The installed DuckFlight is older than 0.2.0. The same error comes from `duckflight_user_add`,
`duckflight_token_add`, `duckflight_accounts` and `duckflight_import`. Check the version with
`select extension_version from duckdb_extensions() where extension_name = 'duckflight';`. On an
older build, users and tokens come from the
[config file](CONFIGURATION.md#legacy-users-and-tokens) instead.

### `duckflight_serve` does not exist

The installed DuckFlight is older than 0.2.0. Update it, or start listeners the older way with
`duckflight_pg_serve(address, config_file)` and `duckflight_flight_serve(address, config_file)`.
DuckDB 1.5.6 receives DuckFlight 0.1.12 with the PostgreSQL and Flight SQL listeners only; HTTP
and Quack need DuckDB 2.0.0 or later.

### `duckflight_pg_serve` does not exist

DuckFlight 0.2.0 replaced the four per-protocol functions with
[`duckflight_serve(protocol, address)`](SQL_FUNCTIONS.md#starting).

## Starting a listener

| Error | Cause | Fix |
| --- | --- | --- |
| `Failed to read DuckFlight config: <path>` | The file is missing or cannot be read | Check the path, which is relative to the DuckDB process's working directory. Leave `config_file` out to serve without a file |
| `Failed to parse DuckFlight config: <path>` | The file is not valid, or has a section this version does not support, such as `[acl]` on DuckFlight 0.1.20 | Run `select * from duckflight_config_check('<path>')` and remove unsupported sections |
| `DuckFlight extension listeners require at least one user, token, OIDC provider, mTLS identity, or initialized account store` | The account store has no user, and the config file configures no OIDC provider or mTLS identity | [Create the first administrator](AUTHENTICATION.md#create-the-first-administrator) with `duckflight_init` |
| `DuckFlight extension listeners require at least one user, token, OIDC provider, or mTLS identity in the configuration file` | A build before 0.2.0, such as the Docker image, found no credentials in the config file | [Add a user or token](AUTHENTICATION.md#the-helper) to the file |
| `DuckFlight http extension listeners require a token, OIDC provider, or mTLS identity; SCRAM users are not supported` | The same on a build before 0.2.0, for HTTP and Quack, which do not accept passwords | Add a token to the file with the [helper](AUTHENTICATION.md#the-helper) |
| `DuckFlight refuses non-loopback listener 0.0.0.0:5433 without TLS` | The address is reachable from other machines and there is no `[tls]` section | Bind `127.0.0.1`, or [add a certificate](AUTHENTICATION.md#tls) |
| `DuckFlight http extension listeners require a token, OIDC provider, mTLS identity, or initialized account store; SCRAM users are not supported` | The same for HTTP and Quack, which do not accept passwords | Run `duckflight_init`, then give clients [tokens](AUTHENTICATION.md#tokens) |
| `all listeners on one DuckFlight host must use the same account store` | This listener's config file resolves to a different [account store](AUTHENTICATION.md#where-it-lives) than the listeners already running | Start every listener with config files that name the same store, or none |
| `store database parent directory must be private (0700)` | The directory holding the account store, from `[store] path` or `acl.store`, can be read by other users | `chmod 700` the directory, or name a directory that does not exist yet so DuckFlight creates it |
| `Address already in use` | Another process, or an earlier listener, holds the port | Pick another port, or stop the old listener with `duckflight_stop` |
| `unsupported DuckFlight extension protocol: <name>` | `duckflight_serve` or `duckflight_stop` was given an unknown protocol | Use `postgres`, `flight`, `http` or `quack` |

`duckflight_stop` returning `no pgwire server on <address>` means nothing is listening at exactly
that address. Copy the address from `duckflight_servers()`; a listener started on a hostname or on
port `0` is registered under the resolved address.

The legacy [helper](AUTHENTICATION.md#the-helper) reports `unexpected top-level field(s): store`
for a file with a `[store]` table, which it does not manage. That message comes from the helper,
not the listener; check the file with `duckflight_config_check` instead.

## Connecting

### Connection refused

Nothing is listening on that address. The server lives inside the DuckDB process that called
`duckflight_serve`, so it stops when that process exits or its session is closed. Confirm with
`duckflight_servers()` that the listener is still registered, and wait for `duckflight_serve` to
return its row before connecting.

### `FATAL:  Password authentication failed for user ""`

The PostgreSQL password is wrong, or the user does not exist. The message can show an empty user
name even when one was sent. Check the user with `show users` as an administrator, or with
`select * from duckflight_accounts()` in the host. A user in the `[users]` table of the config file
is [ignored](#users-or-tokens-from-the-config-file-stop-working) once the account store has a
user.

### `FATAL:  TLS is required`

The PostgreSQL listener has a `[tls]` section and the client connected without TLS. Use
`sslmode=verify-full` with `sslrootcert` pointing at the issuing CA.

### `Invalid username or password` over Flight SQL

Same causes as the PostgreSQL password error. If you meant to use a bearer token, send it in the
`authorization` header instead of the `username` and `password` options; see
[Clients](CLIENTS.md#python-with-a-bearer-token).

### HTTP `401`

The `Authorization: Bearer` header is missing, or the token does not exist: it was mistyped, it or
its user was dropped, or it is a `[tokens]` entry of the config file that the initialized account
store ignores. HTTP never accepts a user name and password.

### `Capability does not grant required scope: query:mutate`

The credential can read but not write. HTTP returns this with status `403`. A token has its user's
[role](AUTHENTICATION.md#roles); give the user the `writer` role with
`alter user <name> set role writer`. Sessions pick up the new role at their next statement.

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

The token passed to `quack_query` or `attach` does not exist, or is a `[tokens]` entry of the
config file that the initialized account store ignores.

### Quack: `Quack authorization changed; reconnect before continuing`

The session's user changed role or tenant, or the user or the session's token was dropped. Quack
sessions do not pick up account changes; open a new connection.

### Quack: `Quack session SQL is outside its fixed capability`

A write through a catalog attached with `type quack` used a token whose user is a `reader`. On
DuckFlight 0.1.20 and earlier, attaching itself needs `query:mutate`, even for reads; from 0.1.21 a
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

## Accounts

### `the account store at <path> is already initialized with <n> user(s)`

`duckflight_init` runs only once per store, and this store already has users, for example from an
earlier run in the same directory. Sign in as an existing administrator, or add users with
`duckflight_user_add('<name>')` in the host or `CREATE USER` over a listener. If no administrator's
password is known, create a new administrator from the host with
`select * from duckflight_user_add('<name>', role := 'admin');`.

### `the account store at <path> is not initialized; run duckflight_init('<admin name>') first`

`duckflight_user_add` and `duckflight_token_add` need a store with a user. Run `duckflight_init`
first. `duckflight_import` reports the same for a file with no administrator to import. If the
listeners were started with a config file that names the store, pass the same file as
`config_file`.

### `this host's listeners use the account store at <path>, not <path>`

A setup function was given a `config_file` (or none) that resolves to another store than the
running listeners use. Pass the config file the listeners were started with.

### Users or tokens from the config file stop working

Once the account store has a user, it is the only source of passwords and tokens, and the
`[users]` and `[tokens]` tables of `duckflight.toml` are ignored, also by listeners that are
already running. A listener started with such a file logs a warning saying how many entries it
ignores and that `duckflight_import('<file>')` brings them into the store.
`select * from duckflight_config_check('duckflight.toml')` counts them in `ignored_users` and
`ignored_tokens`. Run the import, then delete the tables from the file; see
[Upgrading from 0.1.x](AUTHENTICATION.md#upgrading-from-01x).

### `the account store at <path> is initialized, so [users] entries in <file> are ignored`

`duckflight_user_create` and `duckflight_token_create` write to the config file, which the
initialized store would ignore, so they refuse. Use `duckflight_user_add` and
`duckflight_token_add`, or `CREATE USER` and `CREATE TOKEN` over a listener.

### `the store must keep at least one administrator with a credential`

The change would leave the store without an administrator who has a password or a token: dropping
the last admin, changing its role, or removing its last credential. Create or promote another
administrator first, for example with `create user ops role admin` or
`alter user bob set role admin`, then repeat the change.

### `DuckFlight server and setup functions can only be called from the process that owns the database`

A client connected through a listener called a server or setup function such as `duckflight_init`,
`duckflight_user_add` or `duckflight_accounts`. These run only in the DuckDB process that loaded
the extension, whatever the client's role. Over a listener, an administrator uses
[account SQL](AUTHENTICATION.md#manage-accounts-with-sql) instead: `CREATE USER`, `CREATE TOKEN`,
`SHOW USERS`.

### `permission denied: account statements require a store administrator`

Account statements need a store user with the `admin` role. OIDC and mTLS identities cannot run
them, even with the `admin` role. Other users can only change their own password, and only in a
session that signed in with that password.

### `account statements cannot run inside a transaction`

The client opened a transaction first. Psycopg and SQLAlchemy do so by default; enable autocommit
for account statements. Send each statement on its own: a batch such as
`select 1; create user bob` is refused with `Account statements must be executed individually`.

### `CREATE USER` or `SHOW USERS` behaves like plain DuckDB SQL

Until the account store has a user, listeners pass these statements to DuckDB: `SHOW USERS`
describes a table named `users` if there is one, and `CREATE USER` fails with a DuckDB error.
Run `duckflight_init` in the host first.

## Session diagnostics return placeholders

`duckflight_current_user()` returning `duckflight` and `duckflight_pg_stat_activity()` returning no
rows means the query ran in the host DuckDB session rather than through a PostgreSQL client. See
[what to expect](SQL_FUNCTIONS.md#what-to-expect).

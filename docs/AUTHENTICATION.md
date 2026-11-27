# Authentication and TLS

Every DuckFlight listener requires authentication. Users, passwords and bearer tokens live in the
**account store**, a private database that DuckFlight keeps beside yours and that you manage with
SQL. A separate `duckflight.toml` file holds the settings that are not accounts: TLS, OIDC
providers, timeouts, where the store lives, and access controls.

The account store needs DuckFlight 0.2.0 or later. If you are upgrading from 0.1.x, where users
and tokens were written into `duckflight.toml`, see [Upgrading from 0.1.x](#upgrading-from-01x).

## Rules that always apply

- **No anonymous servers.** A listener needs a user in the account store, an OIDC provider, or an
  mTLS identity. A listener with none of these does not start.
- **TLS outside loopback.** Plaintext is allowed only when the address the operating system
  actually bound is loopback (`127.0.0.0/8` or `::1`). Binding `0.0.0.0`, `::`, or any other
  address requires a `[tls]` section in the config file, and startup fails before the listener
  opens if TLS is missing or invalid.
- **Host SQL is trusted.** Anyone who can run SQL in the DuckDB process that loaded the extension
  can start, list and stop listeners and manage every account. Listener authentication does not
  replace process isolation: on a host that accepts untrusted SQL, give each tenant its own DuckDB
  process and control whether the extension can be installed or loaded.

## Choose a method

| Method | Works with | Sent on the wire | Set up with |
| --- | --- | --- | --- |
| [Password](#passwords) | PostgreSQL, Flight SQL | SCRAM-SHA-256 for PostgreSQL; a Basic header during the Flight handshake, then a server-issued bearer | `duckflight_init`, `CREATE USER` |
| [Bearer token](#tokens) | Flight SQL, HTTP, Quack | The token on every request | `duckflight_token_add`, `CREATE TOKEN` |
| [OIDC](#oidc) | PostgreSQL (SASL OAUTHBEARER), Flight SQL, HTTP, Quack | The provider's access token | `[[oidc]]` in the config file |
| [mTLS](#mtls) | Listeners with TLS | A client certificate, mapped by SHA-256 fingerprint | `[tls]` with `client_ca` and `identities` |

One account can have a password, any number of named tokens, or both, so the same account can
connect over all four listeners: with its password over PostgreSQL and Flight SQL, and with a token
over Flight SQL, HTTP and Quack. HTTP and Quack never accept passwords.

## Accounts

### Create the first administrator

Run this once, in the DuckDB process that will serve the database:

```sql
load duckflight;
select * from duckflight_init('alice');
```

It creates the account store and the user `alice` with the `admin` role, and returns a generated
password. The password is shown only this once; the store keeps a salted verifier, never the
password. To choose the password yourself, put it in a file readable only by you and pass
`password_file`; nothing is returned then:

```sql
select * from duckflight_init('alice', password_file := 'alice.password');
```

Then start listeners. No config file is needed:

```sql
select * from duckflight_serve('postgres', '127.0.0.1:5433');
```

`duckflight_init` works only once per store. To add an administrator later, use
`duckflight_user_add` or `CREATE USER ... ROLE admin`.

### Manage accounts with SQL

An administrator connected through any listener manages accounts with SQL statements. Changes take
effect immediately; there is nothing to restart. For example, from `psql` connected as `alice`:

```sql
create user bob role writer;
create token nightly for user bob;
show users;
```

`CREATE USER` returns the new user's generated password and `CREATE TOKEN` returns the token. Each
is shown once, so copy it into the client's secret manager straight away.

| Statement | What it does | Returns |
| --- | --- | --- |
| `CREATE USER name [PASSWORD 'password' \| NO PASSWORD] [ROLE role] [TENANT 'tenant']` | Adds a user. Without a password clause a password is generated. `NO PASSWORD` makes a user that signs in with tokens only | `user`, `password` (null unless generated) |
| `ALTER USER name SET PASSWORD ['password']` | Replaces the password; generates one when none is given | `user`, `password` (null unless generated) |
| `ALTER USER name DROP PASSWORD` | Removes the password; the user keeps its tokens | |
| `ALTER USER name SET ROLE role` | Changes the user's role | |
| `GRANT ROLE role TO USER name` | Same as `ALTER USER name SET ROLE role` | |
| `ALTER USER name SET TENANT 'tenant'`, `ALTER USER name DROP TENANT` | Sets or clears the tenant used by [row policies](ACCESS_CONTROL.md#mask-a-column-or-filter-by-tenant) | |
| `DROP USER name` | Removes the user and its tokens | |
| `CREATE TOKEN token FOR USER name` | Adds a generated bearer token | `user`, `token_name`, `token` |
| `DROP TOKEN token FOR USER name` | Removes a token | |
| `GRANT ROLE role TO PRINCIPAL 'id'`, `GRANT ROLE role TO GROUP 'provider', 'group'` | Gives an [OIDC or mTLS identity](#oidc-and-mtls-identities) a role | |
| `REVOKE ROLE role FROM PRINCIPAL 'id'`, `REVOKE ROLE role FROM GROUP 'provider', 'group'` | Takes it back | |
| `SHOW USERS` | Lists users | `user`, `role`, `has_password`, `tokens`, `tenant` |
| `SHOW TOKENS [FOR USER name]` | Lists token names; tokens themselves are never stored | `user`, `token_name` |
| `SHOW EXTERNAL ACCESS` | Lists roles granted to OIDC and mTLS identities | `kind`, `provider`, `name`, `role` |

`role` is `reader`, `writer` or `admin`. Names are identifiers: double-quote a name that contains
punctuation or spaces, such as `"ci.loader"`. Passwords and tenants are single-quoted strings.
`REVOKE ROLE ... FROM USER` does not exist; change a user's role with `ALTER USER ... SET ROLE` or
remove the user with `DROP USER`.

A few rules apply to every account statement:

- **Administrators only.** Only a store user with the `admin` role can run them. Any other caller
  gets `permission denied: account statements require a store administrator`. The one exception:
  a user signed in with a password can change their own password with
  `ALTER USER <own name> SET PASSWORD`.
- **One statement at a time, outside a transaction.** Send each statement on its own, with the
  password written as a literal rather than a bind parameter. Psycopg and SQLAlchemy open a
  transaction before the first statement unless you enable autocommit, so enable it for these
  statements. Over HTTP, send them to `POST /streaming-query`.
- **The store keeps an administrator.** A change that would leave no administrator with a password
  or token is refused with `the store must keep at least one administrator with a credential`.
  Create the replacement administrator first.
- **Before `duckflight_init`, these are not account statements.** Until the store has a user, a
  listener passes `CREATE USER`, `SHOW USERS` and the rest to DuckDB, which treats them as its own
  SQL.

### What happens to open sessions

Listeners look accounts up at every login, so a new user or token works at once. Sessions that are
already open check their account at every statement:

- A changed role or tenant applies from the session's next statement.
- A dropped user, a dropped token, or a replaced password ends the sessions that used it at their
  next statement.
- A user who changes their own password keeps the session they changed it in; their other sessions
  that signed in with the old password end.
- A Quack session whose account changes must reconnect; it reports
  `Quack authorization changed; reconnect before continuing`.

### Manage accounts from the host

The same work can be done in the DuckDB process that serves the database, which is useful for
scripts and for recovering access. These functions are reserved for that process: a client
connected through a listener gets an error from them whatever its role.

```sql
select * from duckflight_user_add('carol');
select * from duckflight_user_add('etl', role := 'writer');
select * from duckflight_token_add('etl', 'nightly');
select * from duckflight_accounts();
```

Arguments and result columns are in the [SQL reference](SQL_FUNCTIONS.md#account-store). If every
administrator's password is lost, create a new administrator from the host with
`duckflight_user_add('<name>', role := 'admin')`, sign in as that user, and reset the others.

## Roles

Every store user has one of three built-in roles:

| Role | Allows |
| --- | --- |
| `reader` | Read-only SQL such as `SELECT`, query metadata, and transactions |
| `writer` | Everything `reader` allows, plus SQL that changes data or schema (`INSERT`, `UPDATE`, `CREATE TABLE`) and bulk ingestion |
| `admin` | Everything `writer` allows, plus extension-management SQL where the listener permits it, and the [account statements](#manage-accounts-with-sql). With [SQL access controls](ACCESS_CONTROL.md) enabled, admins can also issue grants |

New users start as `reader` unless a role is given. A token carries its user's role, so to let a
token write, give its user the `writer` role. The built-in roles work on DuckDB 2.0.0 and 2.0.1.
Restricting a role to particular tables, columns or rows is a separate feature, which needs DuckDB
2.0.1; see [SQL access controls](ACCESS_CONTROL.md).

### Authorization scopes

Internally each role is a set of scopes. Scopes are also what [OIDC rules](#oidc) and
[mTLS identities](#mtls) grant, so they appear in the config file. Values are case-sensitive.

| Scope | Permission | In role |
| --- | --- | --- |
| `query:execute` | Run read-only SQL such as `SELECT` and access query metadata. | `reader`, `writer`, `admin` |
| `transaction:manage` | Begin, commit, and roll back transactions through the HTTP and Flight SQL transaction APIs. | `reader`, `writer`, `admin` |
| `query:mutate` | Run SQL that changes data or schema, such as `INSERT`, `UPDATE`, and `CREATE TABLE`. | `writer`, `admin` |
| `query:ingest` | Use the HTTP Arrow IPC and Flight SQL bulk ingestion APIs. | `writer`, `admin` |
| `query:admin` | Run extension-management SQL such as `INSTALL` and `LOAD` where the listener permits it. HTTP and PgWire reject these commands regardless of scope. | `admin` |

Airport and ADBC open a Flight SQL transaction even for reads, which every role allows.

## Passwords

DuckFlight generates passwords with 192 bits of randomness, or takes the one you supply. The store
keeps only a salted SCRAM-SHA-256 verifier. Anyone holding the verifier can guess passwords
offline, so use strong unique passwords and protect the store.

PostgreSQL clients authenticate with SCRAM-SHA-256. Flight SQL clients use the standard ADBC flow
instead: a Basic `authorization` header during the handshake, after which the server issues a
bearer for the session. That flow is deliberately not SCRAM, so never use it over an untrusted
plaintext transport. Server-issued bearers last at most 24 hours and are invalidated when the
listener stops or the client closes its session; ADBC clients then reconnect and repeat the
handshake.

## Tokens

A token belongs to one user and acts with that user's role. DuckFlight generates each token with
256 bits of randomness and returns it once; the store keeps only its SHA-256 digest. Token names
are unique per user and appear in `SHOW TOKENS`, so name them after the client that holds them.

Tokens are accepted by the Flight SQL, HTTP and Quack listeners, sent as
`authorization: Bearer <token>`. The PostgreSQL listener does not accept them; use the user's
password there.

To rotate a token, create the new one, update the client, then drop the old one:

```sql
create token nightly_v2 for user bob;
drop token nightly for user bob;
```

## The account store

### Where it lives

By default the store is a DuckDB file beside the database that loaded the extension:

| Database | Store |
| --- | --- |
| A file, such as `/data/sales.duckdb` | `/data/sales.duckdb.duckflight/store.duckdb` |
| In memory | `.duckflight/store.duckdb` in the DuckDB process's working directory |

It is never inside the served database, and no client SQL can reach it. `duckflight_init` creates
it; the directory is created with mode `0700` and the file with mode `0600`.

To keep it somewhere else, name it in the config file:

```toml
[store]
path = "/var/lib/duckflight/accounts/store.duckdb"
```

A relative path is resolved against the config file's directory. The directory holding the store
must be private (mode `0700`); DuckFlight creates it that way if it does not exist. A store named in
the config file is created when a listener starts with that file.

All listeners of one DuckDB process use the same store. When the config file names the store,
start every listener with that file and pass it to the host functions as `config_file`, so they
use the same store:

```sql
select * from duckflight_init('alice', config_file := 'duckflight.toml');
select * from duckflight_serve('postgres', '127.0.0.1:5433', config_file := 'duckflight.toml');
```

`duckflight_config_check('duckflight.toml')` reports which store a file resolves to in its
`store_path` column.

Only one DuckDB process can have a store file open. When several servers need the same accounts,
keep the store in a DuckLake catalog instead.

### Share accounts with a DuckLake store

With a [DuckLake](https://ducklake.select/) store, several DuckFlight servers read and write the
same accounts. Give every server the same `[store]` settings:

```toml
[store]
catalog = "ducklake:sqlite:/srv/duckflight/store.sqlite"
data_path = "/srv/duckflight/store-data/"
refresh_ms = 1000
```

With a SQLite catalog like this one, every server must be able to open the SQLite file and the
data path, for example on the same machine or on a shared file system.

| Key | Meaning |
| --- | --- |
| `catalog` | The DuckLake `ATTACH` target, starting with `ducklake:` |
| `data_path` | Where DuckLake writes its data files. A relative local path is resolved against the config file's directory |
| `setup` | Array of SQL statements run before the catalog is attached, such as `create secret` for object storage |
| `refresh_ms` | For how many milliseconds a server may reuse its last read of the catalog when it checks an account. Default `1000`, maximum `60000`. With `0`, every check queries the catalog |

DuckLake stores have been tested with a SQLite catalog. PostgreSQL catalogs have not been tested
yet.

Each server opens the store in a separate, private DuckDB instance, and loads the `ducklake`
extension there together with the extension for the catalog's metadata database (`sqlite` in this
example). Both extensions must already be installed on the host or be installable from it.

Before relying on a shared store:

- **Changes from other servers take up to `refresh_ms` to arrive.** A server sees its own changes
  immediately. When another server drops a user or a token, this server notices within the
  refresh interval; until then, an open session of that user can still run statements here.
- **Logins stop when the catalog is unreachable.** Once a server's last good read is older than
  the refresh interval, it refuses logins and session checks until it can read the catalog again.
  It never falls back to stale accounts.
- **Use a catalog the served database does not attach.** If the database you serve has the store's
  catalog attached, the listener refuses to start; the store must stay out of reach of client SQL.
- **Protect the catalog like a password file.** Whoever can read the catalog or its data path can
  read the password verifiers and token digests, so allow only DuckFlight to access them. DuckLake
  also retains old snapshots: the verifier of a dropped user remains readable in them until you
  expire those snapshots.
- **Pass the file to the host functions.** Run `duckflight_init('alice', config_file := '...')`
  once, from any server with these settings.

## TLS

Add one certificate and key to the config file. All listeners share it, each negotiating TLS on its
own port.

```toml
[tls]
cert = "server.crt"
key = "server.key"
```

```sql
select * from duckflight_serve('postgres', '0.0.0.0:5433', config_file := 'duckflight.toml');
```

Relative paths are resolved against the directory containing `duckflight.toml`, not the process
working directory. The certificate's subject alternative names must cover the hostname clients
connect to.

For local testing, a self-signed certificate valid for `localhost` is enough:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout server.key -out server.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 server.key
```

Clients then trust `server.crt` as their CA. In production use a certificate from your own CA or a
public one, and have clients verify it (`sslmode=verify-full` for PostgreSQL).

TLS protects server identity, credentials, queries and results. No listener creates a self-signed
identity on its own.

## OIDC

Add one `[[oidc]]` entry per identity provider to the config file, then give its identities access
with authorization rules, with [built-in roles](#oidc-and-mtls-identities), or both.

```toml
[[oidc]]
name = "company"
issuer = "https://identity.example.com"
audience = "duckflight"
jwks_url = "https://identity.example.com/.well-known/jwks.json"
algorithms = ["RS256"]

[[authorization.rules]]
provider = "oidc:company"
group = "analytics"
scopes = ["query:execute", "transaction:manage"]
```

Each issuer must be unique. DuckFlight verifies signatures against cached JWKS, refreshes when it
sees an unknown key ID, and checks issuer, audience, expiry and any configured claim restrictions.
RS256 and ES256 are supported. Locate keys with an explicit `jwks_url`, an explicit
`discovery_url`, or issuer discovery; `jwks` can instead hold an inline public JWK set. HTTPS is
required, and `allow_http = true` exists only for local test providers.

Every rule must name a configured `oidc:<name>`. The optional `subject`, `group` and `tenant`
selectors must all match for a rule to apply. Matching rules add their
[scopes](#authorization-scopes) together, and an identity that matches no rule and holds no role
gets no query privileges. The provider's own `scope` and `roles` claims never grant DuckFlight
privileges directly.

Flight, HTTP and Quack clients send the provider's access token as `authorization: Bearer <token>`.
PostgreSQL clients that support SASL OAUTHBEARER can use the same providers. OIDC does not remove
the TLS requirement outside loopback.

## mTLS

mTLS is an alternative for deployments that already manage client certificates.

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

Once `client_ca` is set, identity mappings are mandatory and client certificates are required by
default. Set `client_cert_mode = "optional"` only for a deliberately mixed deployment where other
clients authenticate with passwords or bearer tokens. A request presenting both a client
certificate and a bearer token is rejected as ambiguous.

## OIDC and mTLS identities

OIDC and mTLS identities are not store users, but an administrator can give them a built-in role
with account SQL. The role's scopes are added to whatever the config file already grants them.

| Identity | Grant to | Example |
| --- | --- | --- |
| One OIDC user | `PRINCIPAL 'oidc~<issuer>~<subject>'` | `grant role reader to principal 'oidc~https://identity.example.com~alice';` |
| An OIDC group, from the provider's `groups_claim` | `GROUP 'oidc:<name>', '<group>'` | `grant role writer to group 'oidc:company', 'analytics';` |
| An mTLS identity | `PRINCIPAL 'mtls~<subject>'` | `grant role reader to principal 'mtls~reporting-service';` |

When several grants match, the highest role applies. `SHOW EXTERNAL ACCESS` lists the grants, and
`REVOKE ROLE ... FROM PRINCIPAL` or `FROM GROUP` removes one. An OIDC or mTLS identity granted
`admin` gets the admin scopes but cannot run account statements; only store users can.

## Treat the file as credential material

The account store and the config file both hold material an attacker can use.

- Never commit either, attach them to a release, include them in CI artifacts, or bake them into
  an image.
- Limit ownership to the account running DuckDB: mode `0600` for files and `0700` for the store's
  directory. Protect backups, certificate private keys and the client secret store too.
- Store production copies of the config file in a secret manager and provision them only at
  deployment time. Keep development, staging and production separate.
- Do not print passwords, tokens, password verifiers, token digests or the store's contents in
  logs, tickets, terminal recordings or support bundles.

### If something leaks

| What was exposed | What to do |
| --- | --- |
| A user's password | `ALTER USER name SET PASSWORD`. Sessions that signed in with the old password end at their next statement. |
| A token | `DROP TOKEN token FOR USER name`, then create a new one for the client. Sessions that used it end. |
| The store file or DuckLake catalog | Reset every password and replace every token. |
| A certificate private key | Replace the certificate and restart the listeners. |

## Upgrading from 0.1.x

DuckFlight 0.1.x kept users and tokens in the `[users]` and `[tokens]` tables of `duckflight.toml`.
Those tables keep working after the upgrade until the account store is initialized.

One call changes when you install 0.2.0. A single function now starts every listener, so
replace the per-protocol functions:

| 0.1.x | 0.2.0 |
| --- | --- |
| `duckflight_pg_serve(address, file)` | `duckflight_serve('postgres', address, config_file := file)` |
| `duckflight_flight_serve(address, file)` | `duckflight_serve('flight', address, config_file := file)` |
| `duckflight_http_serve(address, file)` | `duckflight_serve('http', address, config_file := file)` |
| `duckflight_quack_serve(address, file)` | `duckflight_serve('quack', address, config_file := file)` |

To move them into the store, run in the host process:

```sql
select * from duckflight_import('duckflight.toml');
```

- Every `[users]` entry becomes a store user with the same password and the `admin` role, because
  0.1.x password users had full access.
- Every `[tokens]` entry joins the store user named by its `subject`, which is created without a
  password if it does not exist. The user's role follows the token's scopes: `admin` for
  `query:admin`, `writer` for `query:mutate` or `query:ingest`, otherwise `reader`. A
  `tenant_id` carries over.
- The import is all or nothing and leaves the file unchanged. Running it again changes nothing:
  entries already in the store with the same credential are skipped. The `note` column says what
  happened to each entry.
- Into an empty store, the import itself initializes the store, which needs an administrator in the
  file: a `[users]` entry or a token with `query:admin`. Otherwise run `duckflight_init` first.

Once the store has a user, it is the only source of passwords and tokens:

- `[users]` and `[tokens]` in the config file are ignored, also by listeners that are already
  running. A listener started with such a file logs a warning naming the entries it ignores.
  `duckflight_config_check` counts them in its `ignored_users` and `ignored_tokens` columns.
  Import them, then delete them from the file.
- `duckflight_user_create` and `duckflight_token_create`, the 0.1.x functions that append to the
  file, refuse, because the store would ignore what they add. Use `duckflight_user_add` and
  `duckflight_token_add`, or account SQL.

### The helper

`scripts/duckflight_auth.py` creates and validates `[users]` and `[tokens]` entries in a config
file. It is for DuckFlight builds that predate the account store, such as the
[Docker image](DOCKER.md) and the DuckDB 1.5.6 build; with the account store, use the SQL above
instead. It does not manage `[store]` tables. Run it from a release tag with
[uv](https://docs.astral.sh/uv/):

```sh
alias duckflight-auth='uv run https://raw.githubusercontent.com/sidequery/duckflight-extension/v0.1.21/scripts/duckflight_auth.py'
```

```sh
duckflight-auth user add alice
duckflight-auth token add airport
duckflight-auth check
```

Every command writes to `duckflight.toml` in the current directory unless you pass `--file PATH`,
prompts for passwords rather than taking them as arguments, and writes the file with mode `0600`.
`token add` prints the raw token once as `token=...`; its token gets `query:execute` and
`transaction:manage` unless you list [scopes](#authorization-scopes) with `--scope`. `--replace`
rotates an existing entry. The
format of both tables is in the [configuration reference](CONFIGURATION.md#legacy-users-and-tokens).

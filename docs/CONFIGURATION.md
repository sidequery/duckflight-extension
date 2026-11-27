# Configuration reference

DuckFlight is configured in four places:

| Where | What it controls | Read when |
| --- | --- | --- |
| The [account store](AUTHENTICATION.md#the-account-store) | Users, passwords, tokens and roles, managed with SQL | Every login and statement |
| [`duckflight.toml`](#duckflighttoml) | TLS, OIDC, authorization rules, session limits, the store's location, access controls | Each listener starts |
| [Telemetry environment variables](#telemetry-environment-variables) | OTLP export and query profiling | `load duckflight` |
| [Container environment variables](#container-environment-variables) | Database path, bind addresses and resources for the Docker image | Container start |

## duckflight.toml

The path is the `config_file` argument of `duckflight_serve`. The file is optional: a
listener on a loopback address that authenticates against the account store needs none.

```sql
select * from duckflight_serve('postgres', '127.0.0.1:5433');
select * from duckflight_serve('postgres', '127.0.0.1:5433', config_file := 'duckflight.toml');
```

Relative paths inside the file are resolved against the file's own directory. Unknown top-level
keys are rejected. Listeners read the file once when they start, so restart a listener after
changing it. Accounts are not in this file and change without a restart.

A file using most sections looks like this:

```toml
shutdown_grace_secs = 30

[store]
path = "accounts/store.duckdb"

[tls]
cert = "server.crt"
key = "server.key"

[[oidc]]
name = "company"
issuer = "https://identity.example.com"
audience = "duckflight"

[[authorization.rules]]
provider = "oidc:company"
group = "analytics"
scopes = ["query:execute", "transaction:manage"]

[flight]
max_sessions = 10000
query_timeout_ms = 0

[pgwire]
cross_catalog_reads = true
```

Top-level keys such as `shutdown_grace_secs` must come before the first table header. To validate
a file the way a listener reads it, run `select * from duckflight_config_check('duckflight.toml');`
in the host; see the [SQL reference](SQL_FUNCTIONS.md#config-file).

### Top level

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `shutdown_grace_secs` | integer | Environment default | Seconds a stopping listener lets admitted queries and result fetches finish before interrupting them. `0` forces immediate shutdown. |

Stopping rejects new work first, then waits out the grace period. Stopping one listener leaves the
others running.

### `[store]`

Where the [account store](AUTHENTICATION.md#the-account-store) lives. Without this table it is a
file beside the served database. Give either `path` or `catalog`, not both. Requires DuckFlight
0.2.0 or later.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `path` | path | Beside the database | A store file. Its directory must be private (mode `0700`) |
| `catalog` | string | None | A DuckLake `ATTACH` target, such as `ducklake:sqlite:/srv/duckflight/store.sqlite`, for a store several servers share |
| `data_path` | string | None | `catalog` only: where DuckLake writes data files. A relative local path is resolved against this file's directory |
| `setup` | array of strings | None | `catalog` only: SQL run before the catalog is attached, such as `create secret` |
| `refresh_ms` | integer, at most 60000 | `1000` | `catalog` only: for how many milliseconds a server may reuse its last read of the catalog when it checks an account. With `0`, every check queries the catalog |

See [Share accounts with a DuckLake store](AUTHENTICATION.md#share-accounts-with-a-ducklake-store)
for what to know before using `catalog`.

All listeners in one DuckDB process must resolve to the same store. When this file names one, pass
the file to the host setup functions as `config_file`.

### `[tls]`

One server identity shared by all listeners. Required for any non-loopback address. See
[TLS](AUTHENTICATION.md#tls) and [mTLS](AUTHENTICATION.md#mtls).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cert` | path | Required | Server certificate chain, PEM |
| `key` | path | Required | Server private key, PEM |
| `client_ca` | path | None | CA that signs client certificates; enables mTLS |
| `client_cert_mode` | `"required"` or `"optional"` | `"required"` when `client_ca` is set | Whether clients must present a certificate |

### `[tls.identities."sha256:<fingerprint>"]`

Maps a client certificate, by SHA-256 fingerprint, to an identity. Mandatory once `client_ca` is
set. The identity's principal is `mtls~<subject>`, which account SQL can also
[grant a role](AUTHENTICATION.md#oidc-and-mtls-identities).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `subject` | string | Required | Identity recorded for auditing |
| `scopes` | array of strings | `["query:execute", "transaction:manage"]` | [Authorization scopes](AUTHENTICATION.md#authorization-scopes) |
| `tenant_id` | string | None | Tenant used by [row policies](ACCESS_CONTROL.md#mask-a-column-or-filter-by-tenant) |

### `[[oidc]]`

One entry per identity provider. See [OIDC](AUTHENTICATION.md#oidc).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | Required | Local name, referenced as `oidc:<name>` |
| `issuer` | URL | Required | Expected `iss` claim; must be unique across providers |
| `audience` | string | One of these two is required | Expected audience |
| `audiences` | array of strings | | Several accepted audiences |
| `jwks_url` | URL | Issuer discovery | Where to fetch signing keys |
| `discovery_url` | URL | Issuer discovery | Explicit OIDC discovery document |
| `jwks` | table | None | Inline public JWK set, instead of fetching |
| `algorithms` | array of strings | `["RS256"]` | Accepted algorithms: `RS256`, `ES256` |
| `allow_http` | boolean | `false` | Permit `http://` provider URLs; local testing only |
| `subject_claims` | array of strings | | Claims to read the subject from |
| `groups_claim` | string | | Claim holding group membership, matched by rule `group` and by `GRANT ROLE ... TO GROUP` |
| `tenant_claim` | string | | Claim holding the tenant, matched by rule `tenant` |
| `roles_claim` | string | | Claim holding provider roles |
| `access_token_type` | string | | Required token type |
| `token_use` | string | | Required `token_use` claim |
| `required_claims` | table | | Additional claims that must be present with these values |

Rows without a default list keys the config accepts; their defaults are set by the core and are not
documented here.

### `[[authorization.rules]]`

Grants scopes to OIDC identities. Account SQL can grant them a
[built-in role](AUTHENTICATION.md#oidc-and-mtls-identities) as well; the two add together. An
identity matching no rule and holding no role can run nothing.

| Key | Type | Meaning |
| --- | --- | --- |
| `provider` | `"oidc:<name>"` | Required. Must name a configured provider |
| `subject` | string | Optional selector on the token subject |
| `group` | string | Optional selector on group membership |
| `tenant` | string | Optional selector on tenant |
| `scopes` | array of strings | Required. Scopes added when every given selector matches |

### `[flight]`

Flight SQL session limits. Values here override the corresponding environment defaults; omitted
keys keep them.

| Key | Type | Meaning |
| --- | --- | --- |
| `max_sessions` | positive integer | Maximum concurrent Flight SQL sessions |
| `session_timeout_ms` | integer | Idle limit for a session. `0` disables it |
| `transaction_timeout_ms` | integer | Idle limit for an open transaction. `0` disables it |
| `query_timeout_ms` | integer | Execution deadline for a query. `0` disables it |

A session can override its own query deadline with the Flight `query_timeout` option or
`set query_timeout = '5s'`; `reset query_timeout` restores the server default.

### `[pgwire]`

Requires DuckFlight 0.1.18 or newer. Earlier versions reject a file containing this table.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cross_catalog_reads` | boolean | `false` | Let read-only queries name tables in other attached catalogs |

By default a PostgreSQL session follows PostgreSQL's rule against cross-database references: naming
a catalog other than the connected database fails with SQLSTATE `3D000`. With this setting on, a
read-only `SELECT` (including joins, subqueries, CTEs and set operations) may name
`catalog.schema.relation` in any attached, non-internal catalog, the same set a client can choose
as its startup database. Two-part and unqualified names still resolve in the connected database.
Writes, DDL, writable CTEs, `COPY`, `EXPLAIN`, and functions or types qualified with another catalog
keep the default rule, and catalog metadata still describes only the connected database.

### `[acl]`

Enables SQL-managed roles. Requires DuckFlight 0.1.21 or later on DuckDB 2.0.1; see
[SQL access controls](ACCESS_CONTROL.md). DuckFlight 0.1.20 rejects a file containing this
table with `Failed to parse DuckFlight config`.

```toml
[acl]
store = "roles.duckdb"
admins = ["local~ops"]
```

| Key | Type | Meaning |
| --- | --- | --- |
| `store` | path | DuckDB file holding roles and grants, created relative to the config file |
| `admins` | array of strings | Required. Principals allowed to issue grants, such as `local~ops` for the store user `ops` |

Roles and accounts always live in one store. Without a `[store]` table, `acl.store` is also the
[account store](AUTHENTICATION.md#the-account-store), so pass this file as `config_file` to the
host setup functions. With `[store] path`, leave `acl.store` out or name the same file; with
`[store] catalog`, leave it out. Store users with the `admin` role can issue grants as well as the
principals in `admins`. A principal in `admins` also needs the `query:admin` scope, which for a
store user means the `admin` role.

All listeners in one DuckDB process must use the same `[acl]` settings.

### Legacy users and tokens

DuckFlight 0.1.x read password users and bearer tokens from `[users.<name>]` and `[tokens.<name>]`
tables in this file. Later versions still read them until the account store is initialized, and
ignore them, with a warning, once it has a user. Move them into the store with
`duckflight_import`; see [Upgrading from 0.1.x](AUTHENTICATION.md#upgrading-from-01x).

```toml
[users.alice]
password_hash = "<64 lowercase hexadecimal characters>"
salt = [<16 decimal byte values from 0 through 255>]
iterations = 10000

[tokens.airport]
sha256 = "<64 lowercase hexadecimal characters>"
subject = "airport"
scopes = ["query:execute", "transaction:manage"]
```

| Key | Meaning |
| --- | --- |
| `users.<name>.password_hash` | `PBKDF2-HMAC-SHA256(password, salt, iterations, output_length=32)`, as 64 hex characters |
| `users.<name>.salt` | 16 integers from 0 to 255 |
| `users.<name>.iterations` | At least 4096; the same for every user in the file |
| `tokens.<name>.sha256` | SHA-256 digest of the raw token, as 64 hex characters |
| `tokens.<name>.subject` | Identity recorded for auditing; defaults to the token name. The import makes it the token's store user |
| `tokens.<name>.scopes` | [Authorization scopes](AUTHENTICATION.md#authorization-scopes); defaults to `["query:execute", "transaction:manage"]` |
| `tokens.<name>.tenant_id` | Tenant used by row policies |

The angle-bracket values describe the format and are not usable credentials. For builds that
predate the account store, generate real entries with the [helper](AUTHENTICATION.md#the-helper).

## Telemetry environment variables

Set these before `load duckflight`; they are read once at load. Everything is off unless
`DUCKFLIGHT_TELEMETRY_ENABLED=true`. The full table, with defaults and ranges, is in
[Observability](OBSERVABILITY.md#configuration).

| Group | Variables |
| --- | --- |
| Export | `DUCKFLIGHT_TELEMETRY_ENABLED`, `DUCKFLIGHT_OTLP_PROTOCOL`, `DUCKFLIGHT_OTLP_ENDPOINT`, `DUCKFLIGHT_OTLP_HEADERS`, `DUCKFLIGHT_EXPORT_TIMEOUT_MS` |
| Signals | `DUCKFLIGHT_TELEMETRY_TRACES`, `DUCKFLIGHT_TELEMETRY_METRICS`, `DUCKFLIGHT_TELEMETRY_LOGS`, `DUCKFLIGHT_TELEMETRY_LOG_LEVEL`, `DUCKFLIGHT_TELEMETRY_FILTER` |
| Identity | `DUCKFLIGHT_SERVICE_NAME`, `DUCKFLIGHT_SERVICE_VERSION`, `DUCKFLIGHT_SERVICE_INSTANCE_ID`, `DUCKFLIGHT_RESOURCE_ATTRIBUTES` |
| Sampling | `DUCKFLIGHT_SAMPLE_RATIO`, `DUCKFLIGHT_METRICS_INTERVAL_MS` |
| Profiles | `DUCKFLIGHT_PROFILE_MODE`, `DUCKFLIGHT_PROFILE_SAMPLE_RATIO`, `DUCKFLIGHT_PROFILE_MIN_DURATION_MS`, `DUCKFLIGHT_PROFILE_MAX_OPERATORS`, `DUCKFLIGHT_PROFILE_MAX_BYTES` |

## Container environment variables

These are read by the launcher in the Docker image, not by the extension. They have no effect when
you load DuckFlight into your own DuckDB process. See [Docker](DOCKER.md#environment-variables).

| Variable | Default |
| --- | --- |
| `DUCKFLIGHT_DATABASE` | `/data/duckflight.duckdb` |
| `DUCKFLIGHT_CONFIG` | `/run/secrets/duckflight.toml` |
| `DUCKFLIGHT_PG_ADDRESS` | `0.0.0.0:5433` |
| `DUCKFLIGHT_FLIGHT_ADDRESS` | `0.0.0.0:31337` |
| `DUCKFLIGHT_MEMORY_LIMIT` | DuckDB default |
| `DUCKFLIGHT_THREADS` | DuckDB default |
| `DUCKFLIGHT_TEMP_DIRECTORY` | DuckDB default |
| `DUCKFLIGHT_INIT_SQL` | Unset |

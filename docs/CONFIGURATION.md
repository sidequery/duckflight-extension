# Configuration reference

DuckFlight is configured in three places:

| Where | What it controls | Read when |
| --- | --- | --- |
| [`duckflight.toml`](#duckflighttoml) | Credentials, TLS, authorization, session limits, access controls | Each listener starts |
| [Telemetry environment variables](#telemetry-environment-variables) | OTLP export and query profiling | `load duckflight` |
| [Container environment variables](#container-environment-variables) | Database path, bind addresses and resources for the Docker image | Container start |

## duckflight.toml

The path is the second argument to every `duckflight_*_serve` function. Relative paths inside the
file are resolved against the file's own directory. Unknown top-level keys are rejected.

A file using every section accepted by the published builds looks like this:

```toml
shutdown_grace_secs = 30

[users.alice]
password_hash = "<64 lowercase hexadecimal characters>"
salt = [<16 decimal byte values from 0 through 255>]
iterations = 10000

[tokens.airport]
sha256 = "<64 lowercase hexadecimal characters>"
subject = "airport"
scopes = ["query:execute", "transaction:manage"]

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

The angle-bracket values describe the format and are not usable credentials; generate real entries
with the [helper](AUTHENTICATION.md#the-helper). Quote names that are not valid bare TOML keys.
Top-level keys such as `shutdown_grace_secs` must come before the first table header.

### Top level

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `shutdown_grace_secs` | integer | Environment default | Seconds a stopping listener lets admitted queries and result fetches finish before interrupting them. `0` forces immediate shutdown. |

Stopping rejects new work first, then waits out the grace period. Stopping one listener leaves the
others running.

### `[users.<name>]`

PostgreSQL and Flight SQL password logins. See [Users](AUTHENTICATION.md#users).

| Key | Type | Meaning |
| --- | --- | --- |
| `password_hash` | 64 hex characters | PBKDF2-HMAC-SHA256 output, 32 bytes |
| `salt` | array of 16 integers, 0 to 255 | Per-user salt |
| `iterations` | integer, at least 4096 | PBKDF2 rounds; must be the same for every user in the file |

### `[tokens.<name>]`

Static bearer tokens for Flight SQL, HTTP and Quack. See
[Bearer tokens](AUTHENTICATION.md#bearer-tokens).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `sha256` | 64 hex characters | Required | SHA-256 digest of the raw token |
| `subject` | string | Token name | Identity recorded for auditing |
| `scopes` | array of strings | `["query:execute", "transaction:manage"]` | [Authorization scopes](AUTHENTICATION.md#authorization-scopes) |
| `tenant_id` | string | None | Tenant used by [row policies](ACCESS_CONTROL.md#mask-a-column-or-filter-by-tenant) |

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
set.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `subject` | string | Required | Identity recorded for auditing |
| `scopes` | array of strings | `["query:execute", "transaction:manage"]` | Authorization scopes |
| `tenant_id` | string | None | Tenant used by row policies |

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
| `groups_claim` | string | | Claim holding group membership, matched by rule `group` |
| `tenant_claim` | string | | Claim holding the tenant, matched by rule `tenant` |
| `roles_claim` | string | | Claim holding provider roles |
| `access_token_type` | string | | Required token type |
| `token_use` | string | | Required `token_use` claim |
| `required_claims` | table | | Additional claims that must be present with these values |

Rows without a default list keys the config accepts; their defaults are set by the core and are not
documented here.

### `[[authorization.rules]]`

Grants scopes to OIDC identities. An identity matching no rule can run nothing.

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

Enables SQL-managed roles. Requires a DuckDB 2.0.1 build; see
[SQL access controls](ACCESS_CONTROL.md). DuckFlight 0.1.20 on DuckDB 2.0.0 rejects a file
containing this table with `Failed to parse DuckFlight config`.

```toml
[acl]
store = "roles.duckdb"
admins = ["local~alice"]
```

| Key | Type | Meaning |
| --- | --- | --- |
| `store` | path | DuckDB file holding roles and grants, created relative to the config file |
| `admins` | array of strings | Principals allowed to issue grants, such as `local~alice` for the user `alice` |

All listeners in one DuckDB process must use the same `[acl]` settings.

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

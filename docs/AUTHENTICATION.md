# Authentication and TLS

Every listener takes the path to one `duckflight.toml` file:

```sql
select * from duckflight_pg_serve('127.0.0.1:5433', '/run/secrets/duckflight.toml');
select * from duckflight_flight_serve('127.0.0.1:31337', '/run/secrets/duckflight.toml');
```

That file is the only source of authentication and TLS settings for listeners started from SQL.
Environment variables used by standalone DuckFlight services are not merged into it and cannot
override it. Listeners read the file once when they start, so restart a listener after changing it.

This page explains how to set each method up. For a table of every key, see the
[configuration reference](CONFIGURATION.md).

## Rules that always apply

- **No anonymous servers.** A listener needs at least one user, bearer token, OIDC provider, or
  mTLS identity. An empty configuration does not start.
- **TLS outside loopback.** Plaintext is allowed only when the address the operating system
  actually bound is loopback (`127.0.0.0/8` or `::1`). Binding `0.0.0.0`, `::`, or any other
  address requires a `[tls]` section, and startup fails before the listener opens if TLS is missing
  or invalid.
- **Host SQL is trusted.** Anyone who can run SQL in the DuckDB process that loaded the extension
  can start, list and stop listeners. Listener authentication does not replace process isolation:
  on a host that accepts untrusted SQL, give each tenant its own DuckDB process and control whether
  the extension can be installed or loaded.

## Choose a method

| Method | Works with | Sent on the wire | Config section |
| --- | --- | --- | --- |
| [User and password](#users) | PostgreSQL, Flight SQL | SCRAM-SHA-256 for PostgreSQL; a Basic header during the Flight handshake, then a server-issued bearer | `[users.*]` |
| [Bearer token](#bearer-tokens) | Flight SQL, HTTP, Quack | The token on every request | `[tokens.*]` |
| [OIDC](#oidc) | PostgreSQL (SASL OAUTHBEARER), Flight SQL, HTTP, Quack | The provider's access token | `[[oidc]]` and `[[authorization.rules]]` |
| [mTLS](#mtls) | Listeners with TLS | A client certificate, mapped by SHA-256 fingerprint | `[tls]` with `client_ca` and `identities` |

Methods can be combined in one file. HTTP and Quack do not accept users and passwords, so a
config containing only `[users.*]` is not enough for those two listeners.

## Creating credentials

The [setup functions](SQL_FUNCTIONS.md#setup-functions) create users and tokens from SQL in the
DuckDB process itself, and return each generated secret once. The helper below does the same from
a shell, and also rotates and removes entries.

## The helper

`duckflight_auth.py` creates and validates the file. It has no dependencies beyond Python 3.11,
writes atomically with mode `0600`, and never takes a credential as a command-line argument. Run it
straight from a release tag with [uv](https://docs.astral.sh/uv/):

```sh
alias duckflight-auth='uv run https://raw.githubusercontent.com/sidequery/duckflight-extension/v0.1.21/scripts/duckflight_auth.py'
```

From a checkout of this repository, `uv run scripts/duckflight_auth.py` is the same program.

```sh doctest
duckflight-auth user add alice
duckflight-auth user list
duckflight-auth user test alice

duckflight-auth token add airport
duckflight-auth token list
duckflight-auth token test airport

duckflight-auth check
```

Every command writes to `duckflight.toml` in the current directory unless you pass `--file PATH`.
`--replace` rotates an existing user or token. `check` validates the whole file, and
`duckflight-auth tls` validates the certificate and key once a `[tls]` section exists.

The helper understands every section of the file (users, tokens, TLS, OIDC, authorization rules,
`[flight]`, `[pgwire]`, `[acl]` and `shutdown_grace_secs`) and preserves them when it rewrites the
file. Comments are not preserved.

## Users

```sh
duckflight-auth user add alice
```

This stores a salted password verifier, never the password:

```toml
[users.alice]
password_hash = "<64 lowercase hexadecimal characters>"
salt = [<16 decimal byte values from 0 through 255>]
iterations = 10000
```

`password_hash` is `PBKDF2-HMAC-SHA256(password, salt, iterations, output_length=32)`. All users in
one file must use the same iteration count. DuckFlight requires at least 4,096 and the helper
generates 10,000 by default. Anyone holding the verifier and salt can guess passwords offline, so
use strong unique passwords and protect the file.

PostgreSQL clients authenticate with SCRAM-SHA-256. Flight SQL clients use the standard ADBC flow
instead: a Basic `authorization` header during the handshake, after which the server issues a
bearer for the session. That flow is deliberately not SCRAM, so never use it over an untrusted
plaintext transport. Server-issued bearers last at most 24 hours and are invalidated when the
listener stops or the client closes its session; ADBC clients then reconnect and repeat the
handshake.

## Bearer tokens

```sh
duckflight-auth token add airport
```

The command prints the raw token once as `token=...`. Copy it straight into the client's secret
manager; only its SHA-256 digest is written to the file:

```toml
[tokens.airport]
sha256 = "<64 lowercase hexadecimal characters>"
subject = "airport"
scopes = [
  "query:execute",
  "transaction:manage",
]
```

Helper-generated tokens carry 256 bits of randomness, which is why a plain digest is appropriate.
Do not put a human-chosen password in a token entry. `subject` is the name recorded for auditing
and defaults to the token name.

### Authorization scopes

Scopes limit what a token, mTLS identity or OIDC rule may do. Values are case-sensitive.

| Scope | Permission |
| --- | --- |
| `query:execute` | Run read-only SQL such as `SELECT` and access query metadata. |
| `query:mutate` | Run SQL that changes data or schema, such as `INSERT`, `UPDATE`, and `CREATE TABLE`. |
| `query:ingest` | Use the HTTP Arrow IPC and Flight SQL bulk ingestion APIs. |
| `transaction:manage` | Begin, commit, and roll back transactions through the HTTP and Flight SQL transaction APIs. |
| `query:admin` | Run extension-management SQL such as `INSTALL` and `LOAD` where the listener permits it. HTTP and PgWire reject these commands regardless of scope. |

List every permission the client needs. A client that reads and writes over Flight SQL needs both
`query:execute` and `query:mutate`.

When `scopes` is omitted from a token or mTLS identity it defaults to
`["query:execute", "transaction:manage"]`, which is also what `token add` writes. Airport and ADBC
open a Flight SQL transaction even for reads, so both are needed for the least-privilege read path.
This still denies mutation, ingestion and administrative SQL.

```sh doctest
duckflight-auth token add loader --scope query:execute --scope query:mutate --scope transaction:manage
duckflight-auth token add admin --full-access
```

OIDC identities start with no scopes and receive only those granted by matching rules.

## TLS

Add one certificate and key. All listeners share it, each negotiating TLS on its own port.

```toml
[tls]
cert = "server.crt"
key = "server.key"
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
duckflight-auth tls
```

Clients then trust `server.crt` as their CA. In production use a certificate from your own CA or a
public one, and have clients verify it (`sslmode=verify-full` for PostgreSQL).

TLS protects server identity, credentials, queries and results. No listener creates a self-signed
identity on its own.

## OIDC

Add one `[[oidc]]` entry per identity provider, then grant scopes with authorization rules.

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
selectors must all match for a rule to apply. Matching rules add their scopes together, and an
identity matching no rule gets no query privileges. The provider's own `scope` and `roles` claims
never grant DuckFlight privileges directly.

Flight, HTTP and Quack clients send the provider's access token as `authorization: Bearer <token>`.
PostgreSQL clients that support SASL OAUTHBEARER can use the same providers. OIDC does not remove
the TLS requirement outside loopback.

The helper checks the structure of these tables and that a usable signing key can be selected.
Cryptographic validation and provider availability are checked at runtime; the helper does not
fetch tokens.

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

## Treat the file as credential material

- Never commit it, attach it to a release, include it in CI artifacts, or bake it into an image.
- Store production copies in a secret manager and provision them only at deployment time. Keep
  development, staging and production files separate.
- Limit ownership to the account running DuckDB, with mode `0600`. Protect parent directories,
  backups, certificate private keys and the client secret store too.
- Mount the deployed copy read-only where possible. Run the helper against a writable
  administrative copy, validate it, then publish a new secret-manager version.
- Do not print the file, password hashes, salts, token hashes, raw tokens or passwords in logs,
  tickets, terminal recordings or support bundles.

### If something leaks

| What was exposed | What to do |
| --- | --- |
| The whole file | Rotate every user with a new password and salt, and rotate every bearer token. |
| One raw bearer token | Replace that token entry with `token add NAME --replace` and update its client. |
| Either | Stop and restart the listener to invalidate Flight sessions already issued. |

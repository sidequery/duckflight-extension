# Client authentication and TLS

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

## Secure defaults

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

## Use the helper

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

## Configuration schema

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

### Users

`password_hash` is the 32-byte value produced by
`PBKDF2-HMAC-SHA256(password, salt, iterations, output_length=32)`. Store that derived value, never a
plaintext password. All users in one file must use the same iteration count. DuckFlight requires at
least 4,096 iterations and the helper generates 10,000 by default.

Possession of the verifier and salt permits offline password guessing. Prefer strong unique
passwords even though plaintext passwords are never stored.

### Bearer tokens

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

### Optional mTLS

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

## Treat the file as credential material

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

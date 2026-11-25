# Clients

Every example below talks to the same host: a DuckDB session that has loaded DuckFlight and started
the relevant listener. The examples assume the loopback setup from the
[quickstart](../README.md#quickstart), with a user `alice`, plus a bearer token for the protocols
that need one:

```sh doctest
uv run https://raw.githubusercontent.com/sidequery/duckflight-extension/v0.1.20/scripts/duckflight_auth.py token add demo
```

The command prints `token=...` once. Only its hash is stored, so copy the value now. The examples
read credentials from the environment so that nothing secret is pasted into code:

```sh
export DUCKFLIGHT_PASSWORD='the password you chose for alice'
export DUCKFLIGHT_TOKEN='the token printed above'
```

```sql doctest=host
create table if not exists trips as
    select * from (values (1, 'Lisbon'), (2, 'Osaka')) t(id, city);

select * from duckflight_pg_serve('127.0.0.1:5433', 'duckflight.toml');
select * from duckflight_flight_serve('127.0.0.1:31337', 'duckflight.toml');
select * from duckflight_http_serve('127.0.0.1:4080', 'duckflight.toml');
```

Listeners read the config file when they start. After adding a user or token, stop and restart the
listener that should see it.

| Protocol | Credential | Loopback URL | With TLS |
| --- | --- | --- | --- |
| PostgreSQL | User and password | `postgresql://alice@127.0.0.1:5433/memory` | add `sslmode=verify-full` |
| Flight SQL | User and password, or bearer token | `grpc://127.0.0.1:31337` | `grpc+tls://host:31337` |
| HTTP | Bearer token | `http://127.0.0.1:4080` | `https://host:4080` |
| Quack | Bearer token | `http://127.0.0.1:4081/quack` | `https://host:4081/quack` |

## PostgreSQL

The database name is the DuckDB catalog to use: `memory` for an in-memory database, or the file
name without its extension for a database file (`sales` for `sales.duckdb`). Any attached catalog
can be chosen.

### psql

```sh doctest
psql "host=127.0.0.1 port=5433 user=alice dbname=memory" -c "select * from trips"
```

`psql` prompts for the password, or reads it from `PGPASSWORD`.

With TLS, verify the server certificate against the CA that issued it:

```sh
psql "host=db.example.com port=5433 user=alice dbname=memory sslmode=verify-full sslrootcert=ca.crt"
```

### Python (Psycopg)

```python doctest
import os

import psycopg

with psycopg.connect(
    "host=127.0.0.1 port=5433 user=alice dbname=memory",
    password=os.environ["DUCKFLIGHT_PASSWORD"],
) as conn:
    print(conn.execute("select * from trips order by id").fetchall())
```

### SQLAlchemy

Use the `psycopg2` driver. SQLAlchemy's `psycopg` (version 3) dialect issues `SAVEPOINT` when it
connects, which DuckDB does not support.

```python doctest
import os

from sqlalchemy import URL, create_engine, text

engine = create_engine(
    URL.create(
        "postgresql+psycopg2",
        username="alice",
        password=os.environ["DUCKFLIGHT_PASSWORD"],
        host="127.0.0.1",
        port=5433,
        database="memory",
    )
)
with engine.connect() as conn:
    print(conn.execute(text("select * from trips order by id")).all())
```

### BI tools and other drivers

Choose the PostgreSQL connector and enter the same host, port, user, password and database.
DuckFlight authenticates PostgreSQL clients with SCRAM-SHA-256, which every current driver
supports. It serves PostgreSQL catalog metadata so tools can list tables, columns and types.

SQL sent over this protocol passes through a PostgreSQL compatibility layer, so common PostgreSQL
functions and catalog queries work even where DuckDB's own dialect differs. See the
[SQL reference](SQL_FUNCTIONS.md#postgresql-compatibility-helpers) for what is covered.

## Arrow Flight SQL (ADBC)

Flight SQL returns Arrow record batches without row-by-row conversion, which makes it the better
choice for large results and dataframe clients.

### Python with a user and password

```python doctest
import os

import adbc_driver_flightsql.dbapi as flightsql

with flightsql.connect(
    "grpc://127.0.0.1:31337",
    db_kwargs={"username": "alice", "password": os.environ["DUCKFLIGHT_PASSWORD"]},
) as conn:
    cur = conn.cursor()
    cur.execute("select * from trips order by id")
    print(cur.fetch_arrow_table())
```

The driver sends the password once in a standard Flight handshake and then uses a bearer issued by
the server, valid for at most 24 hours. Never use this flow over plaintext outside loopback.

### Python with a bearer token

```python doctest
import os

import adbc_driver_flightsql.dbapi as flightsql
from adbc_driver_flightsql import DatabaseOptions

with flightsql.connect(
    "grpc://127.0.0.1:31337",
    db_kwargs={
        DatabaseOptions.AUTHORIZATION_HEADER.value: f"Bearer {os.environ['DUCKFLIGHT_TOKEN']}"
    },
) as conn:
    cur = conn.cursor()
    cur.execute("select count(*) from trips")
    print(cur.fetch_arrow_table())
```

A token created with the helper's defaults can read but not write: an `insert` fails with
`Capability does not grant required scope: query:mutate`. Grant more with `--scope`; see
[authorization scopes](AUTHENTICATION.md#authorization-scopes).

### TLS

Use the `grpc+tls` scheme and pass the issuing CA as PEM text:

```python
import os

import adbc_driver_flightsql.dbapi as flightsql
from adbc_driver_flightsql import DatabaseOptions

conn = flightsql.connect(
    "grpc+tls://db.example.com:31337",
    db_kwargs={
        "username": "alice",
        "password": os.environ["DUCKFLIGHT_PASSWORD"],
        DatabaseOptions.TLS_ROOT_CERTS.value: open("ca.crt").read(),
    },
)
```

### DuckDB to DuckDB with Airport

[Airport](https://query.farm/products/extensions/airport/) is a DuckDB extension that attaches a
Flight server as a catalog, so one DuckDB instance can query another. It sends a preconfigured
bearer token on every call and does not perform the username/password handshake.

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
token in `attach`, application logs, or checked-in SQL. Airport opens a Flight SQL transaction even
for reads, so its token needs both `query:execute` and `transaction:manage`, which are the helper's
defaults.

Install Airport as its own documentation describes. On DuckDB 2.0 it is distributed from
Query.Farm's extension repository rather than DuckDB Community.

## HTTP

The HTTP listener takes JSON requests and returns results as an Arrow IPC stream. It accepts bearer
tokens, OIDC tokens and mTLS identities, but not PostgreSQL users: a config with only `[users.*]`
cannot serve HTTP.

```sh doctest
curl -sS --fail http://127.0.0.1:4080/streaming-query \
  -H "Authorization: Bearer $DUCKFLIGHT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "select * from trips order by id"}' \
  -o trips.arrow
```

The response has content type `application/vnd.apache.arrow.stream`. Read it with any Arrow
library:

```python doctest
import pyarrow.ipc

print(pyarrow.ipc.open_stream(open("trips.arrow", "rb")).read_all())
```

| Endpoint | Purpose | Scope needed |
| --- | --- | --- |
| `POST /streaming-query` | Run a read-only query: `{"query": "..."}` | `query:execute` |
| `POST /streaming-mutate` | Run a statement that changes data: `{"operation": "insert", "statement": "...", "txId": "..."}` | `query:mutate` |
| `POST /streaming-ingest` | Bulk-load Arrow data | `query:ingest` |
| `POST /tx/begin` | Open a transaction; returns `{"tx_id": "..."}` | `transaction:manage` |
| `POST /tx/end` | Commit or roll back a transaction | `transaction:manage` |

A missing or wrong token returns `401`. A token without the needed scope returns `403`.

## Quack

The Quack listener serves DuckDB's binary client protocol at `POST /quack`, with content type
`application/vnd.duckdb`. It is a separate listener from the Arrow IPC API and accepts the same
credentials as HTTP. The bearer token travels inside the protocol's connect message rather than in
an `Authorization` header.

```sql doctest=host
select * from duckflight_quack_serve('127.0.0.1:4081', 'duckflight.toml');
```

## Health checks

The Flight listener answers the standard `grpc.health.v1.Health` `Check` and `Watch` calls for
both the empty service name and `arrow.flight.protocol.FlightService`. These need no bearer token
and expose only readiness, but they do follow the listener's TLS and client-certificate policy.
The service reports serving once the listener has initialized and not-serving while it is stopping.

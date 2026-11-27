# Clients

Every example below talks to the same host: a DuckDB session that has loaded DuckFlight and started
the relevant listener. The examples assume the loopback setup from the
[quickstart](../README.md#quickstart), with the administrator `alice`, plus a bearer token of
`alice` for the protocols that need one. Create the token in the DuckDB session:

```sql doctest=host
select * from duckflight_token_add('alice', 'demo');
```

The `token` column is shown once. Only its hash is stored, so copy the value now. An administrator
connected over `psql` can do the same with `create token demo for user alice;`. The examples read
credentials from the environment so that nothing secret is pasted into code:

```sh
export DUCKFLIGHT_PASSWORD='the password duckflight_init returned for alice'
export DUCKFLIGHT_TOKEN='the token returned above'
```

```sql doctest=host
create table if not exists trips as
    select * from (values (1, 'Lisbon'), (2, 'Osaka')) t(id, city);

select * from duckflight_serve('postgres', '127.0.0.1:5433');
select * from duckflight_serve('flight', '127.0.0.1:31337');
select * from duckflight_serve('http', '127.0.0.1:4080');
```

Listeners look accounts up at every login, so users and tokens created while they run work at once.
A token acts with its user's role; `alice` is an administrator, so her token can read and write.

| Protocol | Credential | Loopback URL | With TLS |
| --- | --- | --- | --- |
| PostgreSQL | Password | `postgresql://alice@127.0.0.1:5433/memory` | add `sslmode=verify-full` |
| Flight SQL | Password or bearer token | `grpc://127.0.0.1:31337` | `grpc+tls://host:31337` |
| HTTP | Bearer token | `http://127.0.0.1:4080` | `https://host:4080` |
| Quack | Bearer token | `quack:127.0.0.1:4081` | `quack:host:4081` |

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

Psycopg opens a transaction before the first statement.
[Account statements](AUTHENTICATION.md#manage-accounts-with-sql) such as `create user` cannot run
inside one, so pass `autocommit=True` to `psycopg.connect` for them.

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

A token has its user's [role](AUTHENTICATION.md#roles). A `reader`'s token can read but not write:
an `insert` fails with `Capability does not grant required scope: query:mutate`. To let it write,
give the user the `writer` role with `alter user <name> set role writer`.

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
  auth_token 'the-token-returned-by-create-token'
);

attach 'grpc+tls://flight.example.com:31337' as remote (type airport);
select * from remote.public.example limit 10;
```

Use a temporary DuckDB secret or a suitably protected persistent secret store. Do not inline the
token in `attach`, application logs, or checked-in SQL. Airport opens a Flight SQL transaction even
for reads, which every role allows.

Install Airport as its own documentation describes. On DuckDB 2.0 it is distributed from
Query.Farm's extension repository rather than DuckDB Community.

## HTTP

The HTTP listener takes JSON requests and returns results as an Arrow IPC stream. It accepts bearer
tokens, OIDC tokens and mTLS identities, but not passwords.

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

| Endpoint | Purpose | Lowest role |
| --- | --- | --- |
| `POST /streaming-query` | Run a read-only query: `{"query": "..."}` | `reader` |
| `POST /streaming-mutate` | Run a statement that changes data: `{"operation": "insert", "statement": "...", "txId": "..."}` | `writer` |
| `POST /streaming-ingest` | Bulk-load Arrow data | `writer` |
| `POST /tx/begin` | Open a transaction; returns `{"tx_id": "..."}` | `reader` |
| `POST /tx/end` | Commit or roll back a transaction | `reader` |

A missing or wrong token returns `401`. A token whose role does not allow the request returns
`403`. An administrator's token can also send
[account statements](AUTHENTICATION.md#manage-accounts-with-sql) such as `create user carol` to
`/streaming-query`.

## Quack (DuckDB to DuckDB)

Quack is DuckDB's own client/server protocol. A second DuckDB instance connects with the official
`quack` extension, so results stay in DuckDB's native format end to end. The listener accepts the
same credentials as HTTP.

A `reader`'s token is enough for `quack_query` and for reading through an attached catalog.
Writing needs a `writer` or `admin` token, such as `alice`'s from above. Start the listener on the
host:

```sql doctest=host
select * from duckflight_serve('quack', '127.0.0.1:4081');
```

From another DuckDB, run a single query:

```sql doctest=duckdb
install quack;
load quack;

select *
from quack_query(
    'quack:127.0.0.1:4081',
    'select * from trips order by id',
    disable_ssl := true,
    token := getenv('DUCKFLIGHT_TOKEN')
);
```

Or attach the server as a catalog and query its tables directly:

```sql doctest=duckdb
load quack;

attach 'quack:127.0.0.1:4081' as remote (
    type quack,
    token getenv('DUCKFLIGHT_TOKEN'),
    disable_ssl true
);

select * from remote.main.trips order by id;
```

`getenv` is available in the DuckDB CLI. Elsewhere, store the token in a secret with
`create secret (type quack, token '...')` and leave `token` out of `attach`.

The URI must start with `quack:`. Whether the client uses TLS is decided by the client, from the
host name:

| Host in the URI | Default | To change it |
| --- | --- | --- |
| `localhost`, `127.0.0.1`, `::1` | Plain HTTP | `disable_ssl := false`, or `ssl_fingerprint`, to use TLS |
| Anything else | HTTPS, verified against the system's certificate authorities | `disable_ssl := true` for plain HTTP |

So a listener with a certificate from a public or otherwise trusted CA needs no TLS options at all.
`ssl_fingerprint` is for certificates the client cannot verify through a CA, such as a self-signed
one: it pins the connection to the certificate with that SHA-256 fingerprint.

```sql
select *
from quack_query(
    'quack:db.example.com:4081',
    'select count(*) from trips',
    token := getenv('DUCKFLIGHT_TOKEN'),
    ssl_fingerprint := 'AB:CD:...'
);
```

```sh
openssl x509 -in server.crt -noout -fingerprint -sha256
```

Inserts, updates, deletes and `create table ... as` work through an attached catalog, including
inserts of rows that exist only on the client. These need DuckFlight 0.1.21; on 0.1.20, attaching
needs `query:mutate` even for reads and a plain `insert` through the catalog fails with
`invalid SQL`.

## Health checks

The Flight listener answers the standard `grpc.health.v1.Health` `Check` and `Watch` calls for
both the empty service name and `arrow.flight.protocol.FlightService`. These need no bearer token
and expose only readiness, but they do follow the listener's TLS and client-certificate policy.
The service reports serving once the listener has initialized and not-serving while it is stopping.

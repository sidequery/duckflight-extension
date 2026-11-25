# DuckFlight

DuckFlight is a DuckDB extension that turns the database you already have open into a server.
Load it, start a listener, and query the same live database from `psql`, PostgreSQL drivers and BI
tools, Arrow Flight SQL (ADBC) clients, or plain HTTP. There is no second service to run and no data
to export or copy: every client reads and writes the database that loaded the extension.

## Quickstart

This takes about five minutes and stays on your own machine. You need DuckDB 2.0 on Linux or macOS
and [uv](https://docs.astral.sh/uv/) to run the credential helper.

**1. Create a user.** Listeners never run without authentication, so start by creating a config
file with one user. The helper prompts for a password and writes `duckflight.toml` in the current
directory.

```sh doctest
uv run https://raw.githubusercontent.com/sidequery/duckflight-extension/v0.1.20/scripts/duckflight_auth.py user add alice
```

**2. Start a PostgreSQL listener.** Run `duckdb` in the same directory and leave it open; the
server lives inside this process.

```sql doctest=host
install duckflight from community;
load duckflight;

create table trips as select * from (values (1, 'Lisbon'), (2, 'Osaka')) t(id, city);

select * from duckflight_pg_serve('127.0.0.1:5433', 'duckflight.toml');
```

The function returns one row with the protocol and bound address once the listener is accepting
connections.

**3. Connect.** From a second terminal, use any PostgreSQL client. `memory` is the name of DuckDB's
in-memory database.

```sh
psql "host=127.0.0.1 port=5433 user=alice dbname=memory"
```

```sql doctest=pg
select * from trips;
insert into trips values (3, 'Quito');
```

The new row is immediately visible in the DuckDB session from step 2.

**4. Stop.** In the DuckDB session:

```sql doctest=host
select * from duckflight_servers();
select * from duckflight_stop('pgwire', '127.0.0.1:5433');
```

Plaintext connections like this one are allowed on loopback addresses only. To accept connections
from other machines, add a TLS certificate: see [Authentication](docs/AUTHENTICATION.md#tls).

## Protocols

All listeners serve the same database and can run at the same time.

| Protocol | Start function | Clients | Credentials accepted |
| --- | --- | --- | --- |
| PostgreSQL wire | `duckflight_pg_serve` | `psql`, Psycopg, JDBC, SQLAlchemy, BI tools | Users, OIDC, mTLS |
| Arrow Flight SQL | `duckflight_flight_serve` | ADBC drivers, DuckDB's Airport extension | Users, bearer tokens, OIDC, mTLS |
| HTTP (Arrow IPC) | `duckflight_http_serve` | Anything that can `POST` JSON and read Arrow | Bearer tokens, OIDC, mTLS |
| Quack | `duckflight_quack_serve` | DuckDB's binary client protocol | Bearer tokens, OIDC, mTLS |

Working connection examples for each are in [Clients](docs/CLIENTS.md).

## Compatibility

DuckFlight runs on Linux and macOS, amd64 and arm64. Windows, WebAssembly and Linux musl are not
supported.

| Your DuckDB | Install from | DuckFlight version | Notes |
| --- | --- | --- | --- |
| 2.0.0 | Community or Sidequery repository | 0.1.20 | All four protocols |
| 1.5.6 | Community | 0.1.12 | PostgreSQL and Flight SQL only |
| 2.0.1 | No working published build | | Community serves 0.1.17, which loads but cannot start its core on 2.0.1. [SQL access controls](docs/ACCESS_CONTROL.md) need a 2.0.1 build |

Check what you have with:

```sql doctest=host
pragma version;
select extension_version from duckdb_extensions() where extension_name = 'duckflight';
```

[Install](docs/INSTALL.md) covers every channel, including the Docker image.

## Documentation

| Guide | Read it when you want to |
| --- | --- |
| [Install](docs/INSTALL.md) | Install from Community, the Sidequery repository, or a release artifact |
| [Clients](docs/CLIENTS.md) | Connect from `psql`, Python, ADBC, Airport, or HTTP |
| [Authentication](docs/AUTHENTICATION.md) | Add users, tokens, TLS, OIDC or mTLS, and handle the config file safely |
| [Configuration reference](docs/CONFIGURATION.md) | Look up a `duckflight.toml` key or environment variable |
| [SQL reference](docs/SQL_FUNCTIONS.md) | Look up a server function, PostgreSQL helper, or session diagnostic |
| [SQL access controls](docs/ACCESS_CONTROL.md) | Restrict roles to tables, columns, masked values or tenant rows |
| [Docker](docs/DOCKER.md) | Run DuckFlight as a standalone container |
| [Kubernetes](charts/duckflight/README.md) | Deploy workers over Iceberg or DuckLake with the Helm chart |
| [Observability](docs/OBSERVABILITY.md) | Export OTLP traces, metrics, logs and query profiles |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Decode an error message |

## Authentication setup

Every listener takes the path to one `duckflight.toml` file holding users, bearer tokens, and the
TLS identity. A listener with no credentials configured will not start, and a listener on a
non-loopback address will not start without TLS. The full guide is in
[Authentication](docs/AUTHENTICATION.md).

## SQL API

Seven table functions control the servers: `duckflight_pg_serve`, `duckflight_flight_serve`,
`duckflight_http_serve`, `duckflight_quack_serve`, `duckflight_servers`, `duckflight_stop`, and
`duckflight_core_status`. Their arguments and results, along with the PostgreSQL compatibility
helpers, are in the [SQL reference](docs/SQL_FUNCTIONS.md).

## Security

DuckFlight's core runs as native code inside the DuckDB process. Anyone who can run SQL in that
process can start, list and stop listeners, so treat host SQL access as administrative access.
Report vulnerabilities through [SECURITY.md](SECURITY.md).

## License

This extension is licensed under the [MIT License](LICENSE). Sidequery also licenses the bundled
core binaries under MIT; the core source code remains private and is not covered by that license.

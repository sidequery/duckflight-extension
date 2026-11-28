# DuckFlight

DuckFlight is a DuckDB extension that turns the database you already have open into a server.
Load it, start a listener, and query the same live database from `psql`, PostgreSQL drivers and BI
tools, Arrow Flight SQL (ADBC) clients, another DuckDB, or plain HTTP. There is no second service to run and no data
to export or copy: every client reads and writes the database that loaded the extension.

## Quickstart

This takes about five minutes and stays on your own machine. You need DuckDB 2.0 on Linux or macOS
and DuckFlight 0.2.0 or later.

**1. Create an administrator.** Listeners never run without authentication. Run `duckdb` and leave
it open; the server lives inside this process. Load DuckFlight, create a table to query, and create
the first user:

```sql doctest=host
install duckflight from community;
load duckflight;

create table trips as select * from (values (1, 'Lisbon'), (2, 'Osaka')) t(id, city);

select * from duckflight_init('alice');
```

`duckflight_init` returns `alice`'s generated password. It is shown only this once, so copy it now.
`alice` has the `admin` role. DuckFlight keeps accounts in a private store beside the database,
here `.duckflight/store.duckdb` in the current directory; see
[Authentication](docs/AUTHENTICATION.md#the-account-store).

**2. Start a PostgreSQL listener.** No config file is needed:

```sql doctest=host
select * from duckflight_serve('postgres', '127.0.0.1:5433');
```

The function returns one row with the protocol and bound address once the listener is accepting
connections.

**3. Connect.** From a second terminal, use any PostgreSQL client and enter `alice`'s password.
`memory` is the name of DuckDB's in-memory database.

```sh
psql "host=127.0.0.1 port=5433 user=alice dbname=memory"
```

```sql doctest=pg
select * from trips;
insert into trips values (3, 'Quito');
```

The new row is immediately visible in the DuckDB session from step 1.

**4. Add a user.** Still in `psql` as `alice`:

```sql doctest=pg
create user bob role writer;
```

The result holds `bob`'s generated password, again shown once. `bob` can connect straight away;
nothing needs restarting. `show users;` lists the accounts.

**5. Stop.** In the DuckDB session:

```sql doctest=host
select * from duckflight_servers();
select * from duckflight_stop('postgres', '127.0.0.1:5433');
```

The account store stays in the directory. When you start DuckDB there again, `alice` and `bob`
still exist, so leave out the `duckflight_init` line; the in-memory `trips` table is gone and needs
creating again.

Plaintext connections like this one are allowed on loopback addresses only. To accept connections
from other machines, add a TLS certificate: see [Authentication](docs/AUTHENTICATION.md#tls).

## Protocols

All listeners serve the same database and can run at the same time.

| Protocol | Name for `duckflight_serve` | Clients | Credentials accepted |
| --- | --- | --- | --- |
| PostgreSQL wire | `postgres` | `psql`, Psycopg, JDBC, SQLAlchemy, BI tools | Passwords, tokens (as the password of `<user>~<token name>`), OIDC, mTLS |
| Arrow Flight SQL | `flight` | ADBC drivers, DuckDB's Airport extension | Passwords, bearer tokens, OIDC, mTLS |
| HTTP (Arrow IPC) | `http` | Anything that can `POST` JSON and read Arrow | Bearer tokens, OIDC, mTLS |
| Quack | `quack` | Another DuckDB, through its `quack` extension | Bearer tokens, OIDC, mTLS |

Working connection examples for each are in [Clients](docs/CLIENTS.md).

## Compatibility

DuckFlight runs on Linux and macOS, amd64 and arm64. Windows, WebAssembly and Linux musl are not
supported.

| Your DuckDB | Install from | DuckFlight version | Notes |
| --- | --- | --- | --- |
| 2.0.1 | Sidequery repository | 0.2.1 | All four protocols and [SQL access controls](docs/ACCESS_CONTROL.md). Community serves 0.1.21 until it publishes 0.2.1 |
| 2.0.0 | Sidequery repository | 0.2.1 | All four protocols. Community serves 0.1.20 for this DuckDB version |
| 1.5.6 | Community | 0.1.12 | PostgreSQL and Flight SQL only |

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
| [Authentication](docs/AUTHENTICATION.md) | Manage users, tokens and roles, or set up TLS, OIDC or mTLS |
| [Configuration reference](docs/CONFIGURATION.md) | Look up a `duckflight.toml` key or environment variable |
| [SQL reference](docs/SQL_FUNCTIONS.md) | Look up a server function, PostgreSQL helper, or session diagnostic |
| [SQL access controls](docs/ACCESS_CONTROL.md) | Restrict roles to tables, columns, masked values or tenant rows |
| [Docker](docs/DOCKER.md) | Run DuckFlight as a standalone container |
| [Kubernetes](charts/duckflight/README.md) | Deploy workers over Iceberg or DuckLake with the Helm chart |
| [Observability](docs/OBSERVABILITY.md) | Export OTLP traces, metrics, logs and query profiles |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Decode an error message |

## Accounts

Users, passwords and bearer tokens live in an account store that DuckFlight keeps beside the
database. Each user has the role `reader`, `writer` or `admin`, and one account works on every
listener: its password over PostgreSQL and Flight SQL, its tokens over all four.
Create the first administrator with `duckflight_init`; after that, an administrator manages
accounts over any listener with SQL such as `CREATE USER`, `CREATE TOKEN` and `ALTER USER`, and
changes apply at once.

The optional `duckflight.toml` file holds listener settings only: TLS, OIDC providers, timeouts,
the store's location, and access controls. A listener on a non-loopback address will not start
without TLS. The full guide is in [Authentication](docs/AUTHENTICATION.md); upgrading from 0.1.x,
where users and tokens lived in `duckflight.toml`, is covered
[there too](docs/AUTHENTICATION.md#upgrading-from-01x).

## SQL API

Four table functions control the servers: `duckflight_serve`, `duckflight_servers`,
`duckflight_stop`, and `duckflight_core_status`. Six more set up accounts from the host: `duckflight_init`,
`duckflight_user_add`, `duckflight_token_add`, `duckflight_accounts`, `duckflight_import` and
`duckflight_config_check`. Apart from `duckflight_core_status`, these run only in the host
process; a client connected through a listener cannot call them. Their arguments and results,
along with the PostgreSQL compatibility helpers, are in the [SQL reference](docs/SQL_FUNCTIONS.md).

## Security

DuckFlight's core runs as native code inside the DuckDB process. Anyone who can run SQL in that
process can start, list and stop listeners and manage every account, so treat host SQL access as
administrative access.
Report vulnerabilities through [SECURITY.md](SECURITY.md).

## License

This extension is licensed under the [MIT License](LICENSE). Sidequery also licenses the bundled
core binaries under MIT; the core source code remains private and is not covered by that license.

# Docker

The image runs DuckFlight as a standalone server: one container, one persistent DuckDB database,
with PostgreSQL on port 5433 and Flight SQL on port 31337. It contains the DuckDB **1.5.6** CLI,
the extension, and a small launcher, on a `scratch` base. It runs as UID/GID `10001` and downloads
nothing at startup.

Published images are at `ghcr.io/sidequery/duckflight-extension` for Linux amd64 and arm64, tagged
`latest` and with the full commit SHA. Pin a commit tag or digest in deployments.

## Run it

Both listeners bind `0.0.0.0` inside the container, so **authentication and TLS are required**,
even when Docker publishes the ports only on host loopback. No default credentials or self-signed
certificates are created.

**1. Prepare a secrets directory** holding `duckflight.toml`, the TLS certificate and its key.
Create them with the [helper](AUTHENTICATION.md#the-helper) and a
[certificate](AUTHENTICATION.md#tls) whose names match what clients will connect to; for local
testing include `localhost` and `127.0.0.1`.

```sh
mkdir -m 700 duckflight-secrets && cd duckflight-secrets
duckflight-auth user add alice
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout server.key -out server.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
printf '\n[tls]\ncert = "server.crt"\nkey = "server.key"\n' >> duckflight.toml
duckflight-auth check
```

The container user must be able to read these files. Keep the config and private key at mode
`0600` owned by UID `10001` with the directory traversable by that UID, or run the container with
`--user UID:GID` matching the files and the data directory.

**2. Start the container.** Mount the whole secrets directory so the relative certificate paths
keep working.

```sh
export DUCKFLIGHT_SECRETS_DIR=/absolute/path/to/duckflight-secrets

docker run -d --name duckflight \
  -p 127.0.0.1:5433:5433 -p 127.0.0.1:31337:31337 \
  --mount type=volume,src=duckflight-data,dst=/data \
  --mount type=bind,src="$DUCKFLIGHT_SECRETS_DIR",dst=/run/secrets,readonly \
  -e DUCKFLIGHT_MEMORY_LIMIT=1GB -e DUCKFLIGHT_THREADS=2 \
  ghcr.io/sidequery/duckflight-extension:latest
```

**3. Wait for `DuckFlight ready`** in `docker logs duckflight`, then connect:

```sh
psql "host=localhost port=5433 user=alice dbname=duckflight sslmode=verify-full sslrootcert=$DUCKFLIGHT_SECRETS_DIR/server.crt"
```

Flight SQL clients use `grpc+tls://localhost:31337` with the same CA. See [Clients](CLIENTS.md).

`docker stop` stops the listeners and closes the database; allow a shutdown timeout that suits your
workload. Invalid configuration or failing startup SQL makes the container exit with a failure
status.

### Compose, or building locally

From a checkout of this repository:

```sh
git submodule update --init extension-ci-tools
docker build -t duckflight:local .
```

```sh
export DUCKFLIGHT_SECRETS_DIR=/absolute/path/to/duckflight-secrets
docker compose -f docker/compose.yaml up --build -d
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUCKFLIGHT_DATABASE` | `/data/duckflight.duckdb` | Persistent database path; `:memory:` for an ephemeral database |
| `DUCKFLIGHT_CONFIG` | `/run/secrets/duckflight.toml` | Mounted authentication/TLS config; never baked into the image |
| `DUCKFLIGHT_PG_ADDRESS` | `0.0.0.0:5433` | PostgreSQL bind address; empty string disables PostgreSQL |
| `DUCKFLIGHT_FLIGHT_ADDRESS` | `0.0.0.0:31337` | Flight SQL bind address; empty string disables Flight |
| `DUCKFLIGHT_MEMORY_LIMIT` | DuckDB default | DuckDB memory limit, e.g. `1GB`; leave room for other process memory |
| `DUCKFLIGHT_THREADS` | DuckDB default | DuckDB worker threads, e.g. `2` |
| `DUCKFLIGHT_TEMP_DIRECTORY` | DuckDB default | Writable spill directory; mount separate storage if needed |
| `DUCKFLIGHT_INIT_SQL` | Unset | Path inside the container to a mounted SQL file, e.g. `/run/secrets/init.sql`; executed before listeners start **on every startup**, so make it idempotent |

At least one protocol must stay enabled. Changing a bind port also means changing Docker's port
mapping. Credentials, token scopes and TLS come only from the TOML file; passwords are never
accepted through environment variables. Use one container per database file, and never share a
writable DuckDB file between processes.

## What clients are allowed to do

The launcher locks the server down before it opens a listener. Client SQL runs with
`enable_external_access=false` and `lock_configuration=true`, and `allowed_configs` is reduced to
`TimeZone`, which PostgreSQL session setup needs.

| Clients can | Clients cannot |
| --- | --- |
| Read and write the database | Read arbitrary local files or remote URLs |
| Set their time zone | Change resource limits or any other setting |
| Use spill storage | Install or load extensions |

Set resource limits through the environment variables or startup SQL, and load any extensions you
need during startup.

### Startup SQL

`DUCKFLIGHT_INIT_SQL` is trusted operator code that runs before the lockdown. Two rules apply:

- It must leave configuration unlocked. The launcher applies the final policy itself, and startup
  fails if configuration is already locked.
- It must not start listeners. The launcher starts them after the policy is in place.

### Letting clients import and export files

Allowlist dedicated files or directories in the startup SQL:

```sql
set allowed_directories = ['/imports'];
set allowed_paths = ['/imports/input.csv'];
```

DuckDB also permits attached database and WAL files and the configured spill directory
automatically. Keep databases, spill storage and every allowed path separate from the
authentication and TLS files. Never allowlist `/run/secrets`, a parent such as `/`, or an alias to
a secret file.

## Offline administration

For unrestricted work, stop the server and open the same volume with the DuckDB CLI:

```sh
docker stop duckflight
docker run --rm -it --mount type=volume,src=duckflight-data,dst=/data \
  --entrypoint duckdb ghcr.io/sidequery/duckflight-extension:latest -unsigned /data/duckflight.duckdb
```

Load `/opt/duckflight/duckflight.duckdb_extension` in that session if you need it. The extension
in the image is built from source and unsigned, which is why the server and this command enable
unsigned extensions.

## Kubernetes

For Iceberg or DuckLake, the [Helm chart](../charts/duckflight/README.md) deploys independent
workers with in-memory DuckDB, external lake catalogs, TLS and bootstrap Secrets, and ephemeral
disk for spill. It uses a Deployment with two replicas by default and creates no persistent
volumes.

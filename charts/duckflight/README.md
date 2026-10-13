# DuckFlight on Kubernetes

This chart runs independent query workers: a Deployment, an internal TCP Service,
and a ServiceAccount. Each pod starts DuckDB in memory and bootstraps its lake
catalog before opening PostgreSQL and Flight SQL listeners. No PVC, StatefulSet,
local catalog, or shared DuckDB file is created. ZeroFS is out of scope.

Use an external Iceberg REST catalog, or a DuckLake PostgreSQL catalog with data in
object storage. Catalog provisioning, backups, and maintenance belong to those
systems. An operator is unnecessary for deploying these workers; reconsider one
if catalog lifecycle or lake maintenance needs reconciliation.

## Install

Prepare two existing Secrets in the deployment namespace:

* `duckflight-auth`: `duckflight.toml` and all relative certificate/key files.
  Use the repository's authentication helper. Set certificate SANs to the actual
  Service DNS names (for this example `lake-duckflight.analytics.svc` and
  `lake-duckflight.analytics.svc.cluster.local`). TLS and authentication are
  required even for internal cluster traffic.
* `duckflight-bootstrap`: `init.sql` with trusted startup configuration. It may
  contain catalog credentials; keep it out of Git and Helm values. Mounting a
  Secret instead of templating credentials also avoids Helm release storage.

Provision them through your secret manager, or from local protected files:

```sh
kubectl create namespace analytics
kubectl -n analytics create secret generic duckflight-auth \
  --from-file=duckflight.toml=/absolute/path/duckflight.toml \
  --from-file=server.crt=/absolute/path/server.crt \
  --from-file=server.key=/absolute/path/server.key
kubectl -n analytics create secret generic duckflight-bootstrap \
  --from-file=init.sql=/absolute/path/init.sql

helm upgrade --install lake ./charts/duckflight --namespace analytics \
  --set image.tag="${DUCKFLIGHT_IMAGE_TAG:?Set a tested image commit tag}" \
  --set auth.existingSecret=duckflight-auth \
  --set bootstrap.existingSecret=duckflight-bootstrap
```

Set `image.tag` to a tested commit tag, or set `image.digest` to pin the manifest
digest. The chart requires an explicit image version. Iceberg requires
`librt.so.1`; the image built from this checkout includes it. Images built before
this fix may omit it. Override `image.repository` and
`image.tag` for an image with preinstalled lake extensions. The base image bundles
DuckFlight but not Iceberg, DuckLake, AWS, or PostgreSQL extensions. `INSTALL` in
startup SQL needs egress to the DuckDB extension repository and downloads them
into ephemeral `/data`; a custom image avoids those downloads on every cold start.

## Lake bootstrap

Load extensions, establish temporary DuckDB secrets, attach the catalog, and
allowlist object-storage prefixes in `init.sql`. The launcher subsequently locks
configuration and disables general external access. Loading an extension or
creating a view alone does not allow remote files: all required metadata/data
paths must remain accessible under that final policy. Never allowlist secret
mounts, `/`, or `/data` (which also holds installed extensions).

The following examples are templates; replace endpoints, catalog names, and
prefixes. Use the catalog's documented credential mechanism and validate it with
the DuckDB/extension versions in your chosen image. Add secrets before `ATTACH`.
These examples intentionally contain no reusable credentials.

### Iceberg

```sql
install httpfs;
load httpfs;
install iceberg;
load iceberg;

-- Create the catalog's temporary Iceberg secret and storage secrets here.
-- OAuth, vended credentials, and cloud-managed catalogs differ by provider.
attach 'warehouse' as lake (
  type iceberg,
  endpoint 'https://catalog.example.com',
  read_only
);
set allowed_directories = ['s3://analytics-bucket/warehouse/'];
set max_temp_directory_size = '8GiB';
```

See [DuckDB Iceberg catalogs](https://duckdb.org/docs/stable/core_extensions/iceberg/iceberg_rest_catalogs).
For catalogs whose runtime requests are checked by DuckDB's filesystem policy,
allowlist their precise required URL prefixes too. Catalog API permissions and
object-store IAM remain authoritative; the path allowlist is not a read-only ACL.

### DuckLake

```sql
install httpfs;
load httpfs;
install postgres;
load postgres;
install ducklake;
load ducklake;

-- Create temporary PostgreSQL and scoped S3 secrets here.
attach 'ducklake:postgres:dbname=ducklake_catalog host=catalog.example.com sslmode=verify-full'
  as lake (data_path 's3://analytics-bucket/ducklake/', read_only);
set allowed_directories = ['s3://analytics-bucket/ducklake/'];
set max_temp_directory_size = '8GiB';
```

Use a shared external PostgreSQL catalog, not a local DuckDB/SQLite metadata
file. See [DuckLake catalog choices](https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database).
For workload identity, annotate the chart's ServiceAccount (or supply an existing
one), configure the provider's token projection, and load its DuckDB credential
provider during bootstrap. The chart does not create cloud roles or policies.

Read-only attachments are the initial recommendation for serving analytics.
Writes must target a catalog/table format that coordinates concurrent commits,
using matching catalog and storage permissions. Ordinary local tables are
pod-local and disappear on restart. More replicas increase concurrent query
capacity; they do not distribute one query across pods.

## Connections, scaling, and lifecycle

The Service balances connections. PostgreSQL sessions stay on their established
TCP connection. Flight tickets, transactions, and server-issued bearer sessions
belong to the originating pod: reuse the same gRPC channel for handshake, query,
and result fetches. Clients that open independent channels need routing affinity
at a suitable gateway. Do not treat the Service as a shared Flight session store.
After pod replacement clients must reconnect and create new sessions; queries
are not transparently migrated. Rolling upgrades have an overlap pod, so bootstrap
must be repeatable and must not run catalog migrations or destructive maintenance.

Tune `replicaCount`, `duckdb.memoryLimit`, `duckdb.threads`, `resources`, and
`spill.sizeLimit`. Spill is disk-backed `emptyDir`, not tmpfs; it counts toward
node ephemeral-storage pressure. Keep DuckDB's limit below the container limit
and set `max_temp_directory_size` below spill capacity in startup SQL. Schedule
workers onto nodes with enough local disk and object-store bandwidth. HPA is
deliberately not installed: scale-down must account for active sessions/queries.

Startup and readiness use a TCP probe of the last enabled listener. They detect
listener availability, not successful lake queries or catalog health. Kubernetes'
native gRPC probe does not support this listener's required TLS; the scratch image
has no shell or gRPC probe client. Use external TLS/authenticated synthetic queries
for end-to-end health. Liveness is omitted to avoid restarting workers solely
because an expensive query or catalog outage slows a probe.

The default 90-second termination window allows SIGTERM-driven listener shutdown.
Tune it above your configured listener drain time, allowing for both protocols;
queries exceeding the window may be interrupted. Secret contents are read on
startup. Rotate them using versioned Secret names in values, or explicitly roll
the Deployment after updating an existing Secret. The chart never reads Secret
contents into Helm templates.

Use TLS-aware PostgreSQL clients and Flight SQL at
`grpc+tls://lake-duckflight.analytics.svc:31337`. Keep the Service internal by
default. For external access use a TCP/TLS-capable gateway; PostgreSQL is not HTTP
and generic HTTP ingress is insufficient.

For the examples above, PostgreSQL clients should connect with database `lake`.
Use catalog-qualified names such as `lake.analytics.events` with both protocols;
this also avoids relying on the connection's default catalog/schema for Iceberg.
PostgreSQL's
database qualifier follows the client's selected database; an unrelated startup
database name such as `duckflight` is not an alias for qualified `lake.*` SQL.

## Optional idle sleep and wake-up

Set `idleScale.enabled=true` to run a small, always-on TCP proxy and zero or one
query worker. PostgreSQL and Flight keep the same public Service, port numbers,
TLS identity, and authentication. The first protocol request starts the worker
and waits for bootstrap/readiness, then the proxy forwards the original bytes.
Allow for cold startup in client connection/RPC deadlines; the proxy's default
wait limit is 600 seconds. Connections without protocol data do not wake a worker.

Use an image built from this chart's matching source revision. It must include
the wake-up proxy and idle-control protocol v1, available in core v0.1.13 onward.
Older images fail the opt-in worker readiness check; missing or invalid activity
evidence never authorizes scale-down.

Create an existing Secret with a private `token` (at least 32 alphanumeric
characters) and `ca.crt` containing the worker's issuing CA or trusted self-signed
certificate. Generate/store it through your secret manager. Both worker and
proxy mount this Secret; the token must never be put in Helm values. The worker's
existing TLS certificate must include the public Service's short DNS name
(`lake-duckflight` in this example), which the proxy verifies when connecting to
the pod's HTTPS control endpoint. Query TLS continues to pass through unchanged,
including client certificates and PostgreSQL CancelRequest connections.

```yaml
idleScale:
  enabled: true
  existingSecret: duckflight-idle
  idleTimeoutSeconds: 3000       # 50 minutes after the worker becomes idle
  coldStartTimeoutSeconds: 600
```

The idle interval begins only after all protected activity is gone. Open TCP
sessions, even idle sessions, keep the worker awake. Detached Flight password
sessions, negotiated sessions, prepared statements, transactions, outstanding
result tickets, active response streams, and unfinished engine/cleanup workers
also prevent sleep. Close Flight sessions explicitly with `CloseSession`; closing
only a gRPC channel does not release server-owned sessions. Existing session and
transaction expiration settings are unchanged. Abandoned state with expiration
disabled can therefore keep a worker awake indefinitely. This is intentional:
the idle timeout never force-closes a session or interrupts a query.

Readiness/startup probes use authenticated HTTPS control checks in this mode;
they do not create query sessions or reset the idle clock. Immediately before
scaling to zero, the core atomically fences both listeners and checks the
remaining activity. The proxy serializes sleep/wake and fences Kubernetes writes
using the Deployment's resource version, including recovery after timeouts or
proxy restarts. A new connection arriving during sleep waits for a fresh worker.
The proxy is a single replica with `Recreate` updates; do not attach an HPA or
another replica controller to either Deployment in this mode.

`replicaCount` applies to normal mode; idle mode always owns zero or one worker.
The chart preserves that desired count on Helm upgrades. Enabling it on an
existing release with live normal-mode workers is rejected: install a fresh
release or deliberately drain and scale the old deployment to zero first.
Explicit rollouts, pod deletion,
node failures, and Kubernetes termination-grace limits remain separate lifecycle
events; the idle controller's safety guarantee applies to its automatic sleep.

The chart installs a dedicated proxy ServiceAccount and a namespace Role limited
to reading/patching its worker Deployment and listing pods. The worker does not
receive Kubernetes API credentials. A NetworkPolicy restricts both worker
protocols and control traffic to the proxy pods; use a CNI that enforces it, and
route clients through the public Service. The private worker Service is not a
client endpoint. Worker memory/CPU requests disappear at sleep, but underlying VM
costs decrease only if your cluster also removes unused nodes.

## Validate

```sh
helm lint charts/duckflight --strict \
  --set image.tag=e2e \
  --set auth.existingSecret=auth --set bootstrap.existingSecret=bootstrap
uv run test/test_helm_chart.py
```

The Helm workflow also builds the checkout's server image and runs
[Kubernetes E2E tests](../../test/kubernetes/README.md) on k3s. Separate Iceberg and
DuckLake jobs deploy a real catalog and S3-compatible object store, install this
chart, and query catalog-backed Parquet through TLS PostgreSQL and Flight SQL.
They check both workers, visibility of new external commits, authentication and
secret-file isolation, and query/reconnect after worker replacement.

Before production, also confirm your provider-specific IAM credential refresh,
TLS names, ephemeral disk capacity, and drain behavior with your workload.

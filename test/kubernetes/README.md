# Kubernetes lake E2E

This suite installs the real DuckFlight Helm chart on k3s via k3d, with two
independent query workers. It uses real services, not a mock DuckFlight core:

* Iceberg: Apache's REST catalog fixture, seeded through PyIceberg.
* DuckLake: PostgreSQL metadata, seeded through an independent DuckDB process.
* Both: SeaweedFS's S3 service with credentialed requests and actual Parquet data.
  DuckLake data inlining is disabled so the test cannot pass by reading only
  PostgreSQL catalog rows.

Each catalog test validates:

1. Helm installation, Kubernetes readiness, non-root Secret mounts, and both
   protocol listeners in the shipped scratch image.
2. Exact rows and aggregates over TLS PostgreSQL/SCRAM and TLS Flight SQL/bearer
   authentication, using Service DNS and direct connections to each worker.
   The standard ADBC Flight SQL driver also queries and fetches Arrow results.
3. Rejection of incorrect credentials, secret-file reads, unlocked configuration,
   and writes to the read-only lake attachment.
4. A new commit by the external writer becoming visible through both protocols
   on both already-running workers.
5. Deletion/replacement of one worker, followed by new client connections and the
   same lake queries, with no PVCs created.

The CI matrix builds the current checkout's server image, rather than using an
old published release. Fixture images and the client base image are digest-pinned;
Python client dependencies are locked. Fixture/catalog traffic is internal HTTP
inside the isolated test cluster; DuckFlight client traffic verifies TLS and the
Service hostname. All credentials are generated per run and are not uploaded.

## Local run

Requires Docker, k3d 5.9.0, Helm, kubectl, OpenSSL, and uv. Initialize the pinned
build-tool submodule before building the server:

```sh
git submodule update --init extension-ci-tools
docker build --tag duckflight:e2e .
docker build --file test/kubernetes/Dockerfile --tag duckflight-e2e-client:local .
uv run --no-project test/kubernetes/run.py --catalog iceberg --image duckflight:e2e
uv run --no-project test/kubernetes/run.py --catalog ducklake --image duckflight:e2e
```

Each invocation creates and deletes a uniquely named cluster. It uses a temporary
kubeconfig and never changes the user's current context. `--cluster NAME` reuses
an existing **disposable k3d test cluster** and removes only its generated test
namespace afterwards. `--k3d PATH` selects an explicit k3d binary. `--image` is
required: CI always supplies its checkout-built image. Older published images
omit the `librt.so.1` dependency needed by Iceberg, which this suite caught.

Failures save redacted pod status, events, and logs to `--artifacts DIRECTORY`
before cleanup. CI uploads those diagnostics. The suite does not claim to test
cloud IAM refresh, production catalog availability, in-flight query migration,
or provider-specific catalog authorization.

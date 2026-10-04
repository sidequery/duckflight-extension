# Observability

The bundled core exports OTLP traces, metrics, and correlated query-completion logs
for PgWire and Flight SQL queries. Sampled DuckDB profiles include numeric execution
and operator statistics. Exporters and profiling are disabled by default.

## Configuration

Set environment variables before loading the extension. Authentication and listener
TLS still use the existing `duckflight.toml` configuration.

```sh
export DUCKFLIGHT_TELEMETRY_ENABLED=true
export DUCKFLIGHT_OTLP_PROTOCOL=http/protobuf
export DUCKFLIGHT_OTLP_ENDPOINT=http://127.0.0.1:4318
export DUCKFLIGHT_SERVICE_NAME=analytics
export DUCKFLIGHT_PROFILE_MODE=detailed
export DUCKFLIGHT_PROFILE_SAMPLE_RATIO=0.05
```

```sql
load duckflight;
select * from duckflight_pg_serve('127.0.0.1:5433', '/path/to/duckflight.toml');
select * from duckflight_flight_serve('127.0.0.1:31337', '/path/to/duckflight.toml');
```

| Environment variable | Default | Meaning |
|---|---|---|
| `DUCKFLIGHT_TELEMETRY_ENABLED` | `false` | Enable exporters |
| `DUCKFLIGHT_OTLP_PROTOCOL` | `grpc` | `grpc` or `http/protobuf` |
| `DUCKFLIGHT_OTLP_ENDPOINT` | `http://localhost:4317` or `:4318` | Collector base URL; HTTP appends `/v1/traces`, `/v1/metrics`, `/v1/logs` |
| `DUCKFLIGHT_OTLP_HEADERS` | empty | Comma-separated `key=value` headers |
| `DUCKFLIGHT_TELEMETRY_TRACES` | `true` | Export traces |
| `DUCKFLIGHT_TELEMETRY_METRICS` | `true` | Export metrics |
| `DUCKFLIGHT_TELEMETRY_LOGS` | `true` | Export query-completion logs |
| `DUCKFLIGHT_SERVICE_NAME` | `duckflight` | Service name |
| `DUCKFLIGHT_SERVICE_VERSION` | crate version | Service version |
| `DUCKFLIGHT_SERVICE_INSTANCE_ID` | generated | Service instance identifier |
| `DUCKFLIGHT_RESOURCE_ATTRIBUTES` | empty | Comma-separated resource `key=value` attributes |
| `DUCKFLIGHT_SAMPLE_RATIO` | `1.0` | Root trace sampling probability; W3C parent sampling is respected |
| `DUCKFLIGHT_METRICS_INTERVAL_MS` | `10000` | Periodic export interval |
| `DUCKFLIGHT_EXPORT_TIMEOUT_MS` | `5000` | Per-export timeout, 1–60000 ms |
| `DUCKFLIGHT_TELEMETRY_FILTER` | `duckflight=info` | Filter for tracing instrumentation |
| `DUCKFLIGHT_TELEMETRY_LOG_LEVEL` | `info` | Minimum completion-log severity; `off/error/warn/info/debug/trace` |
| `DUCKFLIGHT_PROFILE_MODE` | `off` | `off`, `standard`, or `detailed` engine profiling |
| `DUCKFLIGHT_PROFILE_SAMPLE_RATIO` | `0.01` | Independent probability of enabling profiling before execution |
| `DUCKFLIGHT_PROFILE_MIN_DURATION_MS` | `0` | Discard collected profiles below this duration |
| `DUCKFLIGHT_PROFILE_MAX_OPERATORS` | `128` | Maximum exported operator count, 1–4096 |
| `DUCKFLIGHT_PROFILE_MAX_BYTES` | `32768` | Maximum serialized profile size, 256–1048576 bytes |


Configuration is read once at `LOAD`. Both listeners share providers and the host
connection pool. Stopping a listener flushes exporters without shutting them down,
so it can restart; database teardown stops workers before shutting down providers.
The extension adds `duckflight.surface=extension` to resources and does not replace
the host application's global tracing subscriber, providers, or propagator.

Use a collector supporting every enabled signal. Disable unsupported signals with
`DUCKFLIGHT_TELEMETRY_METRICS=false` or `DUCKFLIGHT_TELEMETRY_LOGS=false` when sending
traces directly to a trace-only backend. HTTPS and collector authentication headers
are supported for both transports.

## Measurements

Each logical query has one completion outcome: `completed`, `error`, `cancelled`, or
`client_abandoned`. Retries increment its attempt count; suspended PgWire portals
retain their observation until consumption or close. Flight accepts W3C trace context;
PgWire starts its own traces because that protocol has no header carrier.

Query metrics cover counts, active queries, duration, first-batch latency, attempts,
phases, produced rows/Arrow bytes, and encoded transport delivery. Delivery excludes
TCP/TLS framing and does not acknowledge client application consumption. Pool gauges
report checked-out, open, and maximum connections. Process CPU/RSS describe the entire
embedding process. Query IDs, SQL, users, tables, and operator details are not metric
labels. Exported SQL is bounded and literal-redacted; identifiers remain visible.

Completion logs include IDs, protocol, outcome, duration, attempts, and row/byte counts,
correlated with query spans. They do not export arbitrary application log events or raw
engine errors. Metrics include unsampled queries and work when traces are disabled.
Profile sampling is independent: retained samples contribute summary metrics; operator
trees appear on sampled query traces. Minimum duration filters retention after execution
and does not remove profiling overhead from sampled short queries.

## Safe profile capture

DuckFlight enables `no_output` profiling on a sampled query's actual connection,
executes once, finalizes the result, copies `get_profiling_info()`, then restores
the prior settings before cleanup or connection reuse. It never reruns a query
with `EXPLAIN ANALYZE`. Standard mode captures root/operator measurements;
detailed mode also includes available planner/optimizer measurements.

Only numeric metrics and physical operator types are exported. Free-form operator
names, `EXTRA_INFO`, raw SQL, and paths are omitted. The tree uses preorder nodes
with parent indices and a `truncated` flag when caps remove nodes or measurements.
Errors and cancellations never publish a potentially stale successful profile.

Profiles are safely skipped inside explicit transactions: a failed transaction
can reject every restoration statement until the user rolls it back. If a user
already enabled DuckDB profiling or configured a profiling output path, a
bind-only check leaves that profiler and its output untouched; DuckFlight skips
its own capture. This also protects dormant file output: DuckDB can write a
pending profile when profiling is re-enabled. Transaction
control, indirect `EXECUTE`, configuration statements, and unsplit multi-statement
batches also skip capture. Compound/appender ingestion has lifecycle metrics but
no single SQL profile. PgWire's specialized row-projection decomposition skips
profiling because DuckDB would otherwise expose only its last internal row query.
Ordinary lifecycle metrics/traces/logs continue for these cases.

`duckflight.profile.latency`, `.cpu_time`, `.peak_buffer_memory`, and `.peak_temp_size`
summarize retained samples. `CPU_TIME` is cumulative operator work, not an ordered
CPU flamegraph. Buffer-memory and temporary-disk peaks can reflect shared engine
resources under concurrent queries. Operator aggregates are exported as a
`duckdb.profile` trace event, not fabricated chronological child spans.

Continuous CPU stack profiling remains an external profiler integration. This
implementation exports the stable OTLP traces/metrics/logs signals and DuckDB plan
profiles; it does not emit the separate OpenTelemetry Profiles signal.

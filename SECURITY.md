# Security policy

Please report vulnerabilities privately through GitHub's security advisory interface for this
repository. Do not open a public issue containing exploit details, credentials, or private runtime
artifacts.

Reports should include the affected extension and DuckDB versions, operating system and architecture,
reproduction steps, impact, and whether the issue is in this public shim or the embedded core
payload. Core-provider vulnerabilities may need to be handled by that core's distributor.

Only the latest released extension version is supported with security fixes.

Treat `duckflight.toml` as secret credential material even though it stores SCRAM verifiers and
bearer-token hashes rather than plaintext credentials. Never include it in reports, issues,
repository commits, release assets, or support bundles. See
[docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) for storage, permissions, rotation, and
exposure-response guidance.

DuckFlight assumes SQL callers in the DuckDB process are trusted to start, list, and stop listeners.
Deployments that accept untrusted SQL must isolate tenants at the process boundary and control
extension installation/loading. Every listener requires authentication. Non-loopback listeners also
require TLS and fail synchronously when the shared certificate/key is missing or invalid.

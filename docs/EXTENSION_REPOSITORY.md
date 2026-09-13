# Sidequery's signed extension repository

The public repository is `https://extensions.sidequery.dev`, backed directly by the
`sidequery-duckdb-extensions` R2 bucket in Sidequery Production. The R2 custom domain
requires TLS 1.2 or newer. A Worker and DuckDB's community signing service are not
needed.

## Current availability

DuckFlight `0.1.5-alpha` is published for **macOS arm64**, under both
`v2.0.0-alpha41489` and `v2.0.0-alpha41533`. Use **alpha41489** for a fresh HTTPS
installation: its matching official `httpfs` is available. At validation time,
alpha41533's runtime was downloadable but its upstream extension deployment was
still queued, so automatic `httpfs` installation returned 404. Both native
runtimes reported source revision `10de957379`.

The rebuilt private core uses modern lambda syntax in its compatibility macros
and generated catalog views, and recognizes exact trusted legacy definitions.
The actual bundled core passed authenticated PostgreSQL and Flight SQL client
checks on both runtimes, with signatures enforced for repository installation.
Initial `load` took about one to two minutes on the validation host.

Exact official alpha41489 CLI and shared-library artifacts can be retrieved with
an authenticated GitHub CLI. These endpoints return tar.gz content:

```sh
gh api repos/duckdb/duckdb/actions/artifacts/10287349741/zip > duckdb-cli-osx-universal.tar.gz
gh api repos/duckdb/duckdb/actions/artifacts/10287434612/zip > duckdb-shared-libs-osx-universal.tar.gz
```

The CLI archive SHA-256 is
`6a8fd6cc2cb62c5c35804bfc76c5313dea7002cec74074850e786c8953f02108`.
Check `pragma version` after extraction; moving-channel downloads can advance to
an alpha whose matching core extensions are not yet published.

The existing 1.5.5 Community distribution is unchanged.

## Trust the repository

On a compatible DuckDB alpha runtime:

```sql
set allow_extension_repositories = 'allowed';
create extension repository sidequery
    with prefix 'https://extensions.sidequery.dev';

select repository_name, prefix, key_fingerprints
from duckdb_extension_repositories()
where repository_name = 'sidequery';
```

Compare the returned fingerprint with:

```text
sha256:3913381c7400db553886bcf98a0f5dddaed632a3502e8cf618d40ecabc4ee24d
```

DuckDB fetches the PEM public key from
[`/.well-known/duckdb-extension-repo.json`](https://extensions.sidequery.dev/.well-known/duckdb-extension-repo.json)
and pins it locally. Keep `allow_unsigned_extensions=false`.

For the published runtime and platform:

```sql
install duckflight from sidequery;
load duckflight from sidequery;
select * from duckflight_core_status();
```

`loaded` must be `true`. Loading the shim alone does not establish that its core
or servers work. Always include `from sidequery` when loading from this repository.

## Signing key

The RSA-2048 private key is stored in the **Sidequery** 1Password vault in
**DuckDB Extension Repository Signing Key**, field `private_key`. The item also
contains `public_key` and `fingerprint`. The preparation tool uses that item's
stable 1Password reference by default.

Only public-key metadata belongs in `repository/` and R2. The signing tool reads
the private key through `op read` into process memory, passes it to OpenSSL through
stdin, and verifies that it matches the checked-in public key. It never writes the
private key to an artifact or temporary file. This setup does not copy the key to
GitHub Actions secrets.

## Prepare and publish an extension

Prerequisites: `uv`, OpenSSL, an authenticated 1Password CLI, and Wrangler
authenticated to Sidequery Production. Build an artifact with the correct ABI
metadata and validate it on the exact target runtime before signing.

DuckDB alpha's V2 unstable C API requires a different entrypoint from this shim.
The verified ABI for this shim is `C_STRUCT` with minimum V1 API
`v1.5.6`, using the pinned `extension-ci-tools` metadata writer. This API version
is distinct from the runtime-specific repository directory. Do not rewrite an
existing unstable artifact's footer to bypass the version check.

```sh
uv run scripts/extension_repository.py prepare build/alpha/duckflight.duckdb_extension \
  --output build/repository --version "$DUCKDB_REPOSITORY_VERSION"
```

Set `DUCKDB_REPOSITORY_VERSION` to the exact directory requested by the target
runtime, not an assumed release label. Development builds may use a source
revision. The published directories are `v2.0.0-alpha41489` and
`v2.0.0-alpha41533`, and the tested platform is `osx_arm64`. The output is:

```text
build/repository/<runtime-directory>/<platform>/duckflight.duckdb_extension.gz
```

Preparation preserves ABI metadata, refuses existing signatures and output files,
uses DuckDB's two-level SHA-256 hash and RSA PKCS#1 v1.5 signature, verifies the
result, and writes deterministic gzip. It does not upload anything.

Before publication, verify signed install and load with unsigned extensions
disabled, core initialization, and authenticated PostgreSQL and Flight SQL
queries. Publish only the tested objects:

```sh
wrangler whoami
wrangler r2 object put sidequery-duckdb-extensions/.well-known/duckdb-extension-repo.json \
  --file repository/.well-known/duckdb-extension-repo.json \
  --content-type application/json --cache-control 'public, max-age=300' --remote

wrangler r2 object put "sidequery-duckdb-extensions/$DUCKDB_REPOSITORY_VERSION/$DUCKDB_PLATFORM/duckflight.duckdb_extension.gz" \
  --file "build/repository/$DUCKDB_REPOSITORY_VERSION/$DUCKDB_PLATFORM/duckflight.duckdb_extension.gz" \
  --content-type application/gzip --cache-control 'public, max-age=300' --remote
```

The `.gz` object is a compressed download; do not set HTTP `Content-Encoding: gzip`.
Verify the public HTTPS bytes against the prepared object's SHA-256 after upload.

## Validation and upstream references

```sh
uv run test/test_extension_repository.py

# Also exercise signature verification in the actual alpha engine:
DUCKDB_ALPHA_LIBRARY=/path/to/libduckdb.dylib uv run test/test_extension_repository.py

# Real core, authenticated PgWire and Flight SQL clients, isolated temporary trust:
DUCKDB_ALPHA_LIBRARY=/path/to/libduckdb.dylib \
  uv run test/test_alpha_duckflight.py --repository https://extensions.sidequery.dev

# Before signing, validate a freshly built local artifact explicitly:
DUCKDB_ALPHA_LIBRARY=/path/to/libduckdb.dylib \
  DUCKFLIGHT_ALPHA_EXTENSION=build/alpha/duckflight.duckdb_extension \
  uv run test/test_alpha_duckflight.py --allow-unsigned
```

The optional engine test compiles a small native no-op fixture. It proves signed
installation, loading, persisted trust, and tamper rejection with unsigned
extensions disabled; it does not test the DuckFlight core. Without the library
environment variable, that integration test is explicitly skipped.

The real-core client test uses a default 120-second child-process deadline, temporary
authentication credentials, and ephemeral loopback listeners. It checks core
initialization, shared host data, PostgreSQL commits/rollbacks and authentication,
Flight authentication and queries, compatibility macros, and schema discovery.
Repository mode always requires valid signatures.
Set `DUCKFLIGHT_ALPHA_TEST_TIMEOUT=240` on a heavily loaded host; the test remains
bounded. A signed run hit the default deadline under high load, then passed with
the same artifact when load subsided.

### Published build provenance

The public shim is based on `bb078b8`. The private core is based on
`d5cb34897b3f679d3f72b039fef2686082c2d409`, with the catalog lambda compatibility
patch; this preserves the released FFI source and dependency pins. No ABI footer was
rewritten on an old extension: the shim was rebuilt with the new core and fresh
metadata was appended with the pinned `extension-ci-tools` writer.

The normalized embedded core SHA-256 is
`6b714884e95ed4015eecc1bd21af1bd9b55cdee288e5311a61478281bb067a99`.
The signed gzip artifact SHA-256, identical in both runtime directories, is
`433d8e30cffde0e0a038bb0c51e35a5919ccab55b1f1562e3c15c4ae9eb50e43`.
The core links only system libraries; its expected FFI export and absence of
personal build paths were checked before embedding.

Native builds, signing tests, formatting checks, and actual client checks were
run. The private full Rust test suite and Docker rebuild were deferred while
the host had extreme system load; their added regressions are not claimed as run.

- [External extension repositories](https://github.com/duckdb/duckdb/pull/24777)
- [Pinned alpha extension loader](https://github.com/duckdb/duckdb/blob/10de9573794001c649621013bdd93553b54e00c9/src/main/extension/extension_load.cpp)
- [Cloudflare R2 custom domains](https://developers.cloudflare.com/r2/buckets/public-buckets/)

The isolated alpha CLI archive used for validation had SHA-256
`bf702f47e0cbfc4adf638ba8d407245f06bf36f102def1b7bba752f648936bc5`.
The matching shared-library archive had SHA-256
`db710aecfaf475dbf871f771ac4a459bca9e28d57727c74ead7315cafd09afb7`.
Both came from DuckDB's moving `v2.0-cyanoptera` artifact channel, so future downloads
must be checked for their actual version and revision.

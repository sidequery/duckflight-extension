# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv
FROM rust:1.90-bookworm@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS build
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends python3 unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock build.rs core-assets.lock ./
COPY src src
COPY crates crates
COPY test/mock_core test/mock_core
COPY scripts/fetch-core-release.sh scripts/fetch-core-release.sh
COPY extension-ci-tools/scripts extension-ci-tools/scripts
COPY docker/cli-assets.lock docker/cli-assets.lock
RUN uv venv --python python3 configure/venv \
    && uv pip install --python configure/venv/bin/python duckdb==1.5.6 packaging \
    && configure/venv/bin/python extension-ci-tools/scripts/configure_helper.py --duckdb-platform \
    && configure/venv/bin/python -c "import tomllib; from pathlib import Path; Path('configure/extension_version.txt').write_text(tomllib.loads(Path('Cargo.toml').read_text())['package']['version'])"
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    DUCKFLIGHT_CORE_BUNDLE_PATH="$(./scripts/fetch-core-release.sh)" \
    DUCKDB_EXTENSION_NAME=duckflight DUCKDB_EXTENSION_MIN_DUCKDB_VERSION=v1.5.6 \
    cargo build --locked --release \
    && configure/venv/bin/python extension-ci-tools/scripts/append_extension_metadata.py \
        -l target/release/libduckflight.so -o /build/duckflight.duckdb_extension \
        -n duckflight -dv v1.5.6 -evf configure/extension_version.txt \
        -pf configure/platform.txt --abi-type C_STRUCT_UNSTABLE
RUN set -eu; platform="$(cat configure/platform.txt)"; \
    record="$(awk -v platform="$platform" '$1 == platform {print $2, $3}' docker/cli-assets.lock)"; \
    test -n "$record"; set -- $record; \
    curl --fail --location --retry 3 "$2" -o /tmp/duckdb.zip; \
    printf '%s  /tmp/duckdb.zip\n' "$1" | sha256sum --check -; \
    unzip /tmp/duckdb.zip -d /build/cli
RUN curl --fail --location --retry 3 \
        https://github.com/sidequery/duckflight-extension/releases/download/core-v0.1.6/DUCKFLIGHT_CORE_BINARY_LICENSE.txt \
        -o /build/DUCKFLIGHT_CORE_BINARY_LICENSE.txt \
    && echo '1a501d0c38c91c53e0766a9e8c7910d7d11c0ec65ca872f883d5046ed82d1ee2  /build/DUCKFLIGHT_CORE_BINARY_LICENSE.txt' | sha256sum --check -

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS runtime
COPY --from=uv /uv /usr/local/bin/uv
RUN uv pip install --system --no-cache duckdb==1.5.6 \
    && useradd --uid 10001 --create-home duckflight \
    && mkdir -p /data /opt/duckflight \
    && chown duckflight:duckflight /data
COPY --from=build /build/duckflight.duckdb_extension /opt/duckflight/duckflight.duckdb_extension
COPY --from=build /build/cli/duckdb /usr/local/bin/duckdb
COPY docker/server.py /opt/duckflight/server.py
COPY LICENSE /opt/duckflight/LICENSE
COPY --from=build /build/DUCKFLIGHT_CORE_BINARY_LICENSE.txt /opt/duckflight/DUCKFLIGHT_CORE_BINARY_LICENSE.txt
LABEL org.opencontainers.image.source="https://github.com/sidequery/duckflight-extension"
USER 10001:10001
WORKDIR /data
ENV DUCKFLIGHT_DATABASE=/data/duckflight.duckdb \
    DUCKFLIGHT_CONFIG=/run/secrets/duckflight.toml \
    DUCKFLIGHT_PG_ADDRESS=0.0.0.0:5433 \
    DUCKFLIGHT_FLIGHT_ADDRESS=0.0.0.0:31337 \
    PYTHONUNBUFFERED=1
EXPOSE 5433 31337
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/opt/duckflight/server.py"]

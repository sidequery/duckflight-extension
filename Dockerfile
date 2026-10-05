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
    && strip --strip-unneeded target/release/libduckflight.so \
    && configure/venv/bin/python extension-ci-tools/scripts/append_extension_metadata.py \
        -l target/release/libduckflight.so -o /build/duckflight.duckdb_extension \
        -n duckflight -dv v1.5.6 -evf configure/extension_version.txt \
        -pf configure/platform.txt --abi-type C_STRUCT_UNSTABLE
RUN set -eu; platform="$(cat configure/platform.txt)"; \
    record="$(awk -v platform="$platform" '$1 == platform {print $2, $3}' docker/cli-assets.lock)"; \
    test -n "$record"; set -- $record; \
    curl --fail --location --retry 3 "$2" -o /tmp/duckdb.zip; \
    printf '%s  /tmp/duckdb.zip\n' "$1" | sha256sum --check -; \
    unzip /tmp/duckdb.zip -d /build/cli \
    && strip --strip-unneeded /build/cli/duckdb
RUN curl --fail --location --retry 3 \
        https://github.com/sidequery/duckflight-extension/releases/download/core-v0.1.9/DUCKFLIGHT_CORE_BINARY_LICENSE.txt \
        -o /build/DUCKFLIGHT_CORE_BINARY_LICENSE.txt \
    && echo '5d1a7b3c4e482028e465c78281cc2857ea69636d51eea2faad3f518db9fa8cd8  /build/DUCKFLIGHT_CORE_BINARY_LICENSE.txt' | sha256sum --check -
RUN curl --fail --location --retry 3 https://raw.githubusercontent.com/duckdb/duckdb/v1.5.6/LICENSE \
        -o /build/DUCKDB_LICENSE.txt \
    && echo '7e17fd31249fa875cb3b1c5e05c6c3e99b75509f6a2804ca176c217834de1dcb  /build/DUCKDB_LICENSE.txt' | sha256sum --check -
COPY docker/server.c docker/server.c
COPY LICENSE LICENSE

RUN mkdir -p /runtime/usr/local/bin /runtime/opt/duckflight /runtime/etc/ssl/certs \
        /runtime/data /runtime/tmp \
    && cc -Os -s -Wall -Wextra -Werror docker/server.c -o /runtime/usr/local/bin/duckflight-server \
    && cp /build/cli/duckdb /runtime/usr/local/bin/ \
    && cp /build/duckflight.duckdb_extension LICENSE DUCKFLIGHT_CORE_BINARY_LICENSE.txt DUCKDB_LICENSE.txt /runtime/opt/duckflight/ \
    && cp /usr/share/doc/libc6/copyright /runtime/opt/duckflight/LIBC_COPYRIGHT \
    && cp /usr/share/doc/libstdc++6/copyright /runtime/opt/duckflight/LIBSTDCXX_COPYRIGHT \
    && cp /usr/share/doc/libgcc-s1/copyright /runtime/opt/duckflight/LIBGCC_COPYRIGHT \
    && cp /etc/ssl/certs/ca-certificates.crt /runtime/etc/ssl/certs/ \
    && { ldd /build/cli/duckdb; ldd /build/duckflight.duckdb_extension; ldd /runtime/usr/local/bin/duckflight-server; } \
        | awk '/=> \// {print $3} /^\t\// {print $1}' | sort -u \
        | xargs -I '{}' cp --parents -L '{}' /runtime \
    && printf 'duckflight:x:10001:10001::/data:/sbin/nologin\n' > /runtime/etc/passwd \
    && printf 'duckflight:x:10001:\n' > /runtime/etc/group \
    && chown 10001:10001 /runtime/data \
    && chmod 1777 /runtime/tmp

FROM scratch AS runtime
COPY --from=build /runtime/ /
LABEL org.opencontainers.image.source="https://github.com/sidequery/duckflight-extension"
USER 10001:10001
WORKDIR /data
ENV DUCKFLIGHT_DATABASE=/data/duckflight.duckdb \
    DUCKFLIGHT_CONFIG=/run/secrets/duckflight.toml \
    DUCKFLIGHT_PG_ADDRESS=0.0.0.0:5433 \
    DUCKFLIGHT_FLIGHT_ADDRESS=0.0.0.0:31337 \
    HOME=/data
EXPOSE 5433 31337
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/duckflight-server"]

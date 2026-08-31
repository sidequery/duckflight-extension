#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
  echo "usage: $0 [duckflight-core-repository]" >&2
  exit 2
fi

core_repo="${1:-${DUCKFLIGHT_CORE_REPO:-}}"
if [[ -z "${core_repo}" ]]; then
  echo "set DUCKFLIGHT_CORE_REPO or pass the private DuckFlight repository path" >&2
  exit 2
fi
core_repo="$(cd "${core_repo}" && pwd)"
public_repo="$(pwd -P)"
user_home="${HOME:?HOME must identify the release builder home directory}"
core_manifest="${core_repo}/crates/duckflight-core-ffi/Cargo.toml"
if [[ ! -f "${core_manifest}" ]]; then
  echo "missing DuckFlight core manifest: ${core_manifest}" >&2
  exit 2
fi

case "$(uname -s):$(uname -m)" in
  Darwin:arm64) target_triple="aarch64-apple-darwin" ;;
  Darwin:x86_64) target_triple="x86_64-apple-darwin" ;;
  Linux:aarch64) target_triple="aarch64-unknown-linux-gnu" ;;
  Linux:x86_64) target_triple="x86_64-unknown-linux-gnu" ;;
  MINGW*:x86_64|MSYS*:x86_64) target_triple="x86_64-pc-windows-msvc" ;;
  *)
    echo "unsupported bundled-core host: $(uname -s) $(uname -m)" >&2
    exit 2
    ;;
esac
target_triple="${DUCKFLIGHT_CORE_TARGET:-${target_triple}}"
core_target_dir="${DUCKFLIGHT_CORE_TARGET_DIR:-${core_repo}/target/duckflight-extension-bundle}"
core_remap_prefix="${DUCKFLIGHT_CORE_REMAP_PREFIX:-/src/duckflight-core}"

if [[ "${target_triple}" == *apple-darwin ]]; then
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
  export OPENSSL_STATIC=1
  if [[ -z "${OPENSSL_DIR:-}" ]]; then
    if [[ -d /opt/homebrew/opt/openssl@4 ]]; then
      export OPENSSL_DIR=/opt/homebrew/opt/openssl@4
    elif [[ -d /usr/local/opt/openssl@4 ]]; then
      export OPENSSL_DIR=/usr/local/opt/openssl@4
    else
      echo "set OPENSSL_DIR to a static OpenSSL installation" >&2
      exit 2
    fi
  fi
fi

if command -v mbx >/dev/null 2>&1; then
  cargo_build=(mbx build)
else
  cargo_build=(cargo build)
fi

# Rust file names are observable through panic and tracing metadata even in stripped binaries.
# Remap the private checkout root before compiling a distributable core payload.
core_rustflags="${RUSTFLAGS:+${RUSTFLAGS} }--remap-path-prefix=${user_home}=/build/home --remap-path-prefix=${core_repo}=${core_remap_prefix}"
core_cflags="${CFLAGS:+${CFLAGS} }-ffile-prefix-map=${user_home}=/build/home -fdebug-prefix-map=${user_home}=/build/home -ffile-prefix-map=${core_repo}=${core_remap_prefix} -fdebug-prefix-map=${core_repo}=${core_remap_prefix}"
core_cxxflags="${CXXFLAGS:+${CXXFLAGS} }-ffile-prefix-map=${user_home}=/build/home -fdebug-prefix-map=${user_home}=/build/home -ffile-prefix-map=${core_repo}=${core_remap_prefix} -fdebug-prefix-map=${core_repo}=${core_remap_prefix}"

RUSTFLAGS="${core_rustflags}" CFLAGS="${core_cflags}" CXXFLAGS="${core_cxxflags}" "${cargo_build[@]}" \
  --manifest-path "${core_manifest}" \
  --release \
  --target "${target_triple}" \
  --target-dir "${core_target_dir}"

core_output_dir="${core_target_dir}/${target_triple}/release"
case "${target_triple}" in
  *windows*)
    built_core_library="${core_output_dir}/duckflight_core_ffi.dll"
    core_library="${core_output_dir}/duckflight_core_bundle.dll"
    ;;
  *apple-darwin*)
    built_core_library="${core_output_dir}/libduckflight_core_ffi.dylib"
    core_library="${core_output_dir}/libduckflight_core_bundle.dylib"
    ;;
  *)
    built_core_library="${core_output_dir}/libduckflight_core_ffi.so"
    core_library="${core_output_dir}/libduckflight_core_bundle.so"
    ;;
esac
if [[ ! -f "${built_core_library}" ]]; then
  echo "core library was not produced: ${built_core_library}" >&2
  exit 1
fi

cp "${built_core_library}" "${core_library}"

if [[ "${target_triple}" == *apple-darwin ]]; then
  install_name_tool -id "@rpath/libduckflight_core_ffi.dylib" "${core_library}"
fi

public_rustflags="${RUSTFLAGS:+${RUSTFLAGS} }--remap-path-prefix=${user_home}=/build/home --remap-path-prefix=${public_repo}=/src/duckflight-extension"
if [[ "${target_triple}" == *apple-darwin ]]; then
  public_rustflags="${public_rustflags} -C link-arg=-Wl,-install_name,@rpath/duckflight.duckdb_extension"
fi
public_cflags="${CFLAGS:+${CFLAGS} }-ffile-prefix-map=${user_home}=/build/home -fdebug-prefix-map=${user_home}=/build/home -ffile-prefix-map=${public_repo}=/src/duckflight-extension -fdebug-prefix-map=${public_repo}=/src/duckflight-extension"
public_cxxflags="${CXXFLAGS:+${CXXFLAGS} }-ffile-prefix-map=${user_home}=/build/home -fdebug-prefix-map=${user_home}=/build/home -ffile-prefix-map=${public_repo}=/src/duckflight-extension -fdebug-prefix-map=${public_repo}=/src/duckflight-extension"

RUSTFLAGS="${public_rustflags}" CFLAGS="${public_cflags}" CXXFLAGS="${public_cxxflags}" \
  DUCKFLIGHT_CORE_BUNDLE_PATH="${core_library}" make release

for artifact in "${core_library}" build/release/duckflight.duckdb_extension; do
  if strings "${artifact}" | grep -F "${user_home}" >/dev/null; then
    echo "release-builder home path remains in ${artifact}: ${user_home}" >&2
    exit 1
  fi
  for forbidden_fragment in ".codex" "worktrees"; do
    if strings "${artifact}" | grep -F "${forbidden_fragment}" >/dev/null; then
      echo "private checkout metadata remains in ${artifact}: ${forbidden_fragment}" >&2
      exit 1
    fi
  done
done
echo "bundled extension: build/release/duckflight.duckdb_extension"

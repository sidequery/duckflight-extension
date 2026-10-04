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
host_target="${target_triple}"

# DuckDB metadata and both Cargo invocations must describe the same target.
# The CI tools normally cross-target only macOS, and metadata is read from
# configure/platform.txt rather than directly from DUCKDB_PLATFORM.
platform_target() {
  case "$1" in
    osx_arm64) echo aarch64-apple-darwin ;;
    osx_amd64) echo x86_64-apple-darwin ;;
    linux_arm64) echo aarch64-unknown-linux-gnu ;;
    linux_amd64) echo x86_64-unknown-linux-gnu ;;
    windows_amd64) echo x86_64-pc-windows-msvc ;;
    *) echo "unsupported bundled-core platform: $1" >&2; return 2 ;;
  esac
}

target_platform() {
  case "$1" in
    aarch64-apple-darwin) echo osx_arm64 ;;
    x86_64-apple-darwin) echo osx_amd64 ;;
    aarch64-unknown-linux-gnu) echo linux_arm64 ;;
    x86_64-unknown-linux-gnu) echo linux_amd64 ;;
    x86_64-pc-windows-msvc) echo windows_amd64 ;;
    *) echo "unsupported bundled-core target: $1" >&2; return 2 ;;
  esac
}

platform="${DUCKDB_PLATFORM:-}"
target_triple="${DUCKFLIGHT_CORE_TARGET:-}"
if [[ -n "${platform}" ]]; then
  platform_triple="$(platform_target "${platform}")"
  if [[ -n "${target_triple}" && "${target_triple}" != "${platform_triple}" ]]; then
    echo "DUCKDB_PLATFORM=${platform} conflicts with DUCKFLIGHT_CORE_TARGET=${target_triple}" >&2
    exit 2
  fi
  target_triple="${platform_triple}"
elif [[ -n "${target_triple}" ]]; then
  platform="$(target_platform "${target_triple}")"
else
  target_triple="${host_target}"
  platform="$(target_platform "${target_triple}")"
  if [[ -f configure/platform.txt && "$(<configure/platform.txt)" != "${platform}" ]]; then
    echo "configure/platform.txt does not match host platform ${platform}; set DUCKDB_PLATFORM or rerun make configure" >&2
    exit 2
  fi
fi

# Library filenames and linker setup in the CI tools are selected by the host OS.
if [[ "${target_triple#*-}" != "${host_target#*-}" ]]; then
  echo "bundled cross-OS builds are unsupported: ${host_target} -> ${target_triple}" >&2
  exit 2
fi
if [[ ! -f configure/platform.txt ]]; then
  echo "run make configure before building a bundled extension" >&2
  exit 2
fi

# Refresh even when configure contains a prior cross-build platform. Explicit
# target flags also override Cargo's environment/configured default target.
make "DUCKDB_PLATFORM=${platform}" platform
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

# The native host ownership bridge must use the exact verified DuckDB headers.
# Keep version and checksum selection in the core repository's build helper.
headers_helper="${core_repo}/scripts/prepare-extension-core-headers.sh"
if [[ ! -f "${headers_helper}" ]]; then
  echo "missing DuckFlight core header preparation helper: ${headers_helper}" >&2
  exit 2
fi
core_include_dir="$(bash "${headers_helper}" "${core_target_dir}")"

DUCKFLIGHT_DUCKDB_INCLUDE_DIR="${core_include_dir}" \
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
  DUCKFLIGHT_CORE_BUNDLE_PATH="${core_library}" make \
    "DUCKDB_PLATFORM=${platform}" "TARGET_INFO=--target ${target_triple}" \
    "TARGET_PATH=${CARGO_TARGET_DIR:-./target}/${target_triple}" release

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

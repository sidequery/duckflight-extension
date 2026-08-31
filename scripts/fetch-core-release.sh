#!/usr/bin/env bash
set -euo pipefail

manifest="${DUCKFLIGHT_CORE_ASSET_MANIFEST:-core-assets.lock}"
platform="${DUCKDB_PLATFORM:-}"
if [[ -z "${platform}" && -f configure/platform.txt ]]; then
  platform="$(<configure/platform.txt)"
fi
if [[ -z "${platform}" ]]; then
  echo "DUCKDB_PLATFORM or configure/platform.txt is required" >&2
  exit 2
fi
if [[ ! -f "${manifest}" ]]; then
  echo "core asset manifest is missing: ${manifest}" >&2
  exit 2
fi

record="$(awk -v platform="${platform}" '$1 == platform { print; exit }' "${manifest}")"
if [[ -z "${record}" ]]; then
  echo "no DuckFlight core asset is locked for platform ${platform}" >&2
  exit 2
fi
read -r locked_platform expected_sha256 url extra <<<"${record}"
if [[ "${locked_platform}" != "${platform}" || -z "${expected_sha256}" || -z "${url}" || -n "${extra:-}" ]]; then
  echo "invalid core asset record for platform ${platform}" >&2
  exit 2
fi
case "${url}" in
  https://github.com/*/releases/download/*) ;;
  *)
    echo "core asset URL must be an immutable GitHub Release download" >&2
    exit 2
    ;;
esac

asset_name="${url##*/}"
asset_dir="build/core-assets/${platform}"
asset_path="${asset_dir}/${asset_name}"
mkdir -p "${asset_dir}"
curl --fail --location --retry 3 --output "${asset_path}.download" "${url}"

if command -v sha256sum >/dev/null 2>&1; then
  actual_sha256="$(sha256sum "${asset_path}.download" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  actual_sha256="$(shasum -a 256 "${asset_path}.download" | awk '{print $1}')"
else
  echo "sha256sum or shasum is required to verify the core asset" >&2
  exit 2
fi
if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
  echo "core asset checksum mismatch for ${platform}" >&2
  exit 1
fi
mv "${asset_path}.download" "${asset_path}"
printf '%s\n' "${asset_path}"

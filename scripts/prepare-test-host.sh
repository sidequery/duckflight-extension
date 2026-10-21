#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${DUCKDB_TEST_CLI:-}" ]]; then
  test -x "${DUCKDB_TEST_CLI}"
  printf '%s\n' "${DUCKDB_TEST_CLI}"
  exit
fi

# Test the final release pin, independently of the latest Python nightly wheel.
commit=d54cbbe3df17e8e5ce68312f483b4155836112aa
archive_sha=93e3d1f9c84b6ec3493237d21565c01c2c19b9862e54e5552d3d7999ccc3bbb4
source_dir="$(pwd -P)/build/test-host/${commit}"
if [[ ! -f "${source_dir}/.duckflight-duckdb-commit" ]]; then
  scratch="$(mktemp -d)"
  trap 'rm -rf "${scratch}"' EXIT
  curl --fail --silent --show-error --location --retry 3 \
    "https://codeload.github.com/duckdb/duckdb/tar.gz/${commit}" \
    --output "${scratch}/source.tar.gz"
  if command -v sha256sum >/dev/null 2>&1; then
    actual_sha="$(sha256sum "${scratch}/source.tar.gz" | awk '{print $1}')"
  else
    actual_sha="$(shasum -a 256 "${scratch}/source.tar.gz" | awk '{print $1}')"
  fi
  if [[ "${actual_sha}" != "${archive_sha}" ]]; then
    echo 'DuckDB test host source checksum mismatch' >&2
    exit 1
  fi
  mkdir -p "${source_dir}"
  tar -xzf "${scratch}/source.tar.gz" -C "${source_dir}" --strip-components=1
  printf '%s\n' "${commit}" > "${source_dir}/.duckflight-duckdb-commit"
fi
cmake -S "${source_dir}" -B "${source_dir}/build/release" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_UNITTESTS=OFF -DBUILD_SHELL=ON \
  -DGIT_COMMIT_HASH="${commit}" \
  -DDUCKDB_EXPLICIT_VERSION=v2.0.0-alpha44357 >&2
cmake --build "${source_dir}/build/release" --target duckdb \
  --parallel "${DUCKDB_TEST_BUILD_JOBS:-2}" >&2
printf '%s\n' "${source_dir}/build/release/duckdb"

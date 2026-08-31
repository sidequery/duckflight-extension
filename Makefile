.PHONY: all clean clean_all debug release release_internal test test_debug test_release build_mock_core bundled_release

PROJ_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

EXTENSION_NAME=duckflight
USE_UNSTABLE_C_API=1
TARGET_DUCKDB_VERSION=v1.5.5

ifeq ($(OS),Windows_NT)
MOCK_CORE_LIBRARY=target/debug/duckflight_mock_core.dll
else
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
MOCK_CORE_LIBRARY=target/debug/libduckflight_mock_core.dylib
else
MOCK_CORE_LIBRARY=target/debug/libduckflight_mock_core.so
endif
endif

export DUCKFLIGHT_CORE_PATH := $(abspath $(MOCK_CORE_LIBRARY))
export DUCKFLIGHT_MANAGEMENT_TOKEN := duckflight-test-management

all: configure debug

include extension-ci-tools/makefiles/c_api_extensions/base.Makefile
include extension-ci-tools/makefiles/c_api_extensions/rust.Makefile

configure: venv platform extension_version

debug: build_extension_library_debug build_extension_with_metadata_debug
release:
	@bundle_path="$${DUCKFLIGHT_CORE_BUNDLE_PATH:-}"; \
	if [ -z "$${bundle_path}" ] && grep -Eq '^[^#[:space:]]' core-assets.lock; then \
		bundle_path="$$(./scripts/fetch-core-release.sh)"; \
	fi; \
	if [ -n "$${bundle_path}" ]; then \
		DUCKFLIGHT_CORE_BUNDLE_PATH="$${bundle_path}" $(MAKE) release_internal; \
	else \
		$(MAKE) release_internal; \
	fi
release_internal: build_extension_with_metadata_release

test: test_debug
test_debug: test_extension_debug
test_release: test_extension_release

build_mock_core:
	cargo build --package duckflight-mock-core

bundled_release:
	./scripts/build-bundled.sh "$(DUCKFLIGHT_CORE_REPO)"

test_extension_debug_internal test_extension_release_internal: build_mock_core

clean: clean_build clean_rust
clean_all: clean_configure clean

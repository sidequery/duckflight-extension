"""Exercise repository preparation with ephemeral keys and OpenSSL verification."""

import ctypes
import gzip
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "extension_repository",
    Path(__file__).resolve().parents[1] / "scripts/extension_repository.py",
)
repository = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repository)


class DuckDBResult(ctypes.Structure):
    # Public v1 duckdb.h layout; access values/errors through the C API.
    _fields_ = [
        ("deprecated_column_count", ctypes.c_uint64),
        ("deprecated_row_count", ctypes.c_uint64),
        ("deprecated_rows_changed", ctypes.c_uint64),
        ("deprecated_columns", ctypes.c_void_p),
        ("deprecated_error_message", ctypes.c_void_p),
        ("internal_data", ctypes.c_void_p),
    ]


def extension(platform="osx_arm64", target="v2.0.0-alpha.1", abi="CPP", magic="4"):
    fields = [magic, platform, target, "0.1.0", abi, "", "", ""]
    footer = b"".join(field.encode().ljust(32, b"\0") for field in reversed(fields))
    return b"\xcf\xfa\xed\xfe" + b"payload" * 180_000 + footer + bytes(256)


class ExtensionRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = repository.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ]
        )
        cls.public = repository.run(["openssl", "pkey", "-pubout"], cls.private)
        cls.other = repository.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ]
        )

    def test_signature_matches_independent_openssl_digest_and_detects_tampering(self):
        data = extension()
        signed = repository.sign(data, self.private, self.public)
        self.assertEqual(signed[:-256], data[:-256])
        # Reference hashing uses OpenSSL, including a full chunk and a partial chunk.
        content = data[:-256]
        chunks = [
            subprocess.run(
                ["openssl", "dgst", "-sha256", "-binary"],
                input=part,
                capture_output=True,
                check=True,
            ).stdout
            for part in [content[:1048576], content[1048576:]]
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chunks").write_bytes(b"".join(chunks))
            (root / "public").write_bytes(self.public)
            (root / "signature").write_bytes(signed[-256:])
            result = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(root / "public"),
                    "-signature",
                    str(root / "signature"),
                    str(root / "chunks"),
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(ValueError):
            repository.verify(bytes([signed[0] ^ 1]) + signed[1:], self.public)
        with self.assertRaises(ValueError):
            repository.verify(signed[:-1] + bytes([signed[-1] ^ 1]), self.public)

    def test_rejects_wrong_key_and_existing_signature(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            repository.sign(extension(), self.other, self.public)
        with self.assertRaisesRegex(ValueError, "existing signature"):
            repository.sign(extension()[:-1] + b"x", self.private, self.public)

    def test_metadata_and_path_validation(self):
        for data, version in [
            (b"short", "v2.0.0"),
            (extension(magic="3"), "v2.0.0-alpha.1"),
            (extension(platform="../escape"), "v2.0.0-alpha.1"),
            (extension(), "../escape"),
            (extension(), "v1.5.5"),
            (extension(abi="C_STRUCT_UNSTABLE"), "v1.5.5"),
            (extension(abi="invalid"), "v2.0.0-alpha.1"),
            (extension(abi="C_STRUCT", target="garbage"), "v2.0.0-alpha.1"),
            (extension(platform="osx\0arm64"), "v2.0.0-alpha.1"),
        ]:
            with (
                self.subTest(version=version, footer=data[-512:-256]),
                self.assertRaises(ValueError),
            ):
                repository.metadata(data, version)
        self.assertEqual(
            repository.metadata(
                extension(abi="C_STRUCT", target="v1.2.0"), "v2.0.0-alpha.1"
            ),
            "osx_arm64",
        )

    def test_prepare_preserves_footer_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "duckflight.duckdb_extension"
            artifact.write_bytes(extension())
            public_metadata = root / "metadata.json"
            public_metadata.write_text(
                json.dumps({"signature_keys": [self.public.decode()]})
            )
            original_run = repository.run

            def run(arguments, data=None):
                if arguments[0] == "op":
                    self.assertEqual(arguments, ["op", "read", repository.DEFAULT_KEY])
                    return self.private
                return original_run(arguments, data)

            with patch.object(repository, "run", side_effect=run):
                first = repository.prepare(
                    artifact, root / "first", "v2.0.0-alpha.1", public_metadata
                )
                second = repository.prepare(
                    artifact, root / "second", "v2.0.0-alpha.1", public_metadata
                )
                with self.assertRaises(FileExistsError):
                    repository.prepare(
                        artifact, root / "first", "v2.0.0-alpha.1", public_metadata
                    )
            self.assertEqual(
                first.relative_to(root / "first").as_posix(),
                "v2.0.0-alpha.1/osx_arm64/duckflight.duckdb_extension.gz",
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            signed = gzip.decompress(first.read_bytes())
            self.assertEqual(signed[:-256], artifact.read_bytes()[:-256])
            repository.verify(signed, self.public)
            self.assertFalse(
                any(
                    self.private in path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                )
            )

    def test_optional_alpha_runtime_accepts_signed_fixture_and_rejects_tampering(self):
        library = os.environ.get("DUCKDB_ALPHA_LIBRARY")
        if not library or not Path(library).is_file() or not shutil.which("cc"):
            self.skipTest(
                "set DUCKDB_ALPHA_LIBRARY to an alpha shared library and install cc"
            )
        api = ctypes.CDLL(library)
        pointer = ctypes.c_void_p
        signatures = {
            "duckdb_create_config": ([ctypes.POINTER(pointer)], ctypes.c_int),
            "duckdb_set_config": (
                [pointer, ctypes.c_char_p, ctypes.c_char_p],
                ctypes.c_int,
            ),
            "duckdb_destroy_config": ([ctypes.POINTER(pointer)], None),
            "duckdb_open_ext": (
                [
                    ctypes.c_char_p,
                    ctypes.POINTER(pointer),
                    pointer,
                    ctypes.POINTER(pointer),
                ],
                ctypes.c_int,
            ),
            "duckdb_close": ([ctypes.POINTER(pointer)], None),
            "duckdb_connect": ([pointer, ctypes.POINTER(pointer)], ctypes.c_int),
            "duckdb_disconnect": ([ctypes.POINTER(pointer)], None),
            "duckdb_query": (
                [pointer, ctypes.c_char_p, ctypes.POINTER(DuckDBResult)],
                ctypes.c_int,
            ),
            "duckdb_destroy_result": ([ctypes.POINTER(DuckDBResult)], None),
            "duckdb_result_error": ([ctypes.POINTER(DuckDBResult)], ctypes.c_char_p),
            "duckdb_value_varchar": (
                [ctypes.POINTER(DuckDBResult), ctypes.c_uint64, ctypes.c_uint64],
                pointer,
            ),
            "duckdb_free": ([pointer], None),
        }
        for name, (arguments, result) in signatures.items():
            getattr(api, name).argtypes = arguments
            getattr(api, name).restype = result

        connection = pointer()
        database = pointer()

        def query(sql, expect_error=False):
            result = DuckDBResult()
            try:
                status = api.duckdb_query(
                    connection, sql.encode(), ctypes.byref(result)
                )
                error = (api.duckdb_result_error(ctypes.byref(result)) or b"").decode()
                if expect_error:
                    self.assertNotEqual(status, 0, sql)
                    self.assertIn("signature", error.lower())
                    return None
                self.assertEqual(status, 0, error)
                value = api.duckdb_value_varchar(ctypes.byref(result), 0, 0)
                try:
                    return ctypes.string_at(value).decode() if value else None
                finally:
                    api.duckdb_free(value)
            finally:
                api.duckdb_destroy_result(ctypes.byref(result))

        with tempfile.TemporaryDirectory(prefix="duckflight-runtime-") as directory:
            root = Path(directory)

            def open_database():
                config = pointer()
                error = pointer()
                self.assertEqual(api.duckdb_create_config(ctypes.byref(config)), 0)
                try:
                    for name, value in {
                        "extension_repository_directory": str(root / "trust"),
                        "extension_directories": f"['{root / 'installed'}']",
                        "allow_unsigned_extensions": "false",
                        "allow_extension_repositories": "allowed",
                    }.items():
                        self.assertEqual(
                            api.duckdb_set_config(
                                config, name.encode(), value.encode()
                            ),
                            0,
                            name,
                        )
                    status = api.duckdb_open_ext(
                        None, ctypes.byref(database), config, ctypes.byref(error)
                    )
                    self.assertEqual(
                        status,
                        0,
                        ctypes.string_at(error).decode() if error else "open failed",
                    )
                    self.assertEqual(
                        api.duckdb_connect(database, ctypes.byref(connection)), 0
                    )
                    self.assertEqual(
                        query("select current_setting('allow_unsigned_extensions')"),
                        "false",
                    )
                finally:
                    api.duckdb_free(error)
                    api.duckdb_destroy_config(ctypes.byref(config))

            try:
                open_database()
                platform = query("pragma platform")
                version = query("select library_version from pragma_version()")
                if "dev" in version:
                    version = query("select source_id from pragma_version()")
                source = root / "fixture.c"
                # This is a native no-op test fixture, NOT the real DuckFlight core.
                source.write_text(
                    "#include <stdbool.h>\nbool duckflight_init_c_api(void *info, void *access) { return true; }\n"
                )
                artifact = root / "duckflight.duckdb_extension"
                subprocess.run(
                    ["cc", "-shared", "-fPIC", str(source), "-o", str(artifact)],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                fields = ["4", platform, "v1.5.6", "test", "C_STRUCT", "", "", ""]
                footer = b"".join(
                    field.encode().ljust(32, b"\0") for field in reversed(fields)
                )
                unsigned = artifact.read_bytes() + footer + bytes(256)
                repository.metadata(unsigned, version)
                signed = repository.sign(unsigned, self.private, self.public)
                served = root / "repository"
                metadata_path = served / ".well-known/duckdb-extension-repo.json"
                metadata_path.parent.mkdir(parents=True)
                metadata_path.write_text(
                    json.dumps({"signature_keys": [self.public.decode()]})
                )
                download = (
                    served / version / platform / "duckflight.duckdb_extension.gz"
                )
                download.parent.mkdir(parents=True)
                download.write_bytes(gzip.compress(signed, mtime=0))
                query(f"CREATE EXTENSION REPOSITORY fixture WITH PREFIX '{served}'")
                query("INSTALL duckflight FROM fixture")
                query("LOAD duckflight FROM fixture")
                api.duckdb_disconnect(ctypes.byref(connection))
                api.duckdb_close(ctypes.byref(database))
                open_database()
                query("LOAD duckflight FROM fixture")
                download.write_bytes(
                    gzip.compress(signed[:-1] + bytes([signed[-1] ^ 1]), mtime=0)
                )
                query("FORCE INSTALL duckflight FROM fixture", expect_error=True)
            finally:
                if connection:
                    api.duckdb_disconnect(ctypes.byref(connection))
                if database:
                    api.duckdb_close(ctypes.byref(database))


if __name__ == "__main__":
    unittest.main()

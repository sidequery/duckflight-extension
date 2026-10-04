#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Manage DuckFlight users, bearer tokens, and TLS configuration."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomllib

DEFAULT_ITERATIONS = 10_000
MINIMUM_ITERATIONS = 4_096
DEFAULT_SALT_BYTES = 16
DEFAULT_FILE = "duckflight.toml"
FULL_ACCESS_SCOPES = [
    "query:admin",
    "query:execute",
    "query:ingest",
    "query:mutate",
    "transaction:manage",
]
DEFAULT_SCOPES = ["query:execute", "transaction:manage"]


class AuthFileError(ValueError):
    """An invalid DuckFlight configuration or command request."""


def derive_password_hash(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=32
    ).hex()


def _validated_user(username: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthFileError(f"user {username!r} must be a TOML table")
    expected_fields = {"password_hash", "salt", "iterations"}
    fields = set(value)
    if fields != expected_fields:
        missing = sorted(expected_fields - fields)
        extra = sorted(fields - expected_fields)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise AuthFileError(f"user {username!r}: {'; '.join(details)}")
    password_hash = value["password_hash"]
    if not isinstance(password_hash, str):
        raise AuthFileError(f"user {username!r}: password_hash must be a string")
    try:
        password_hash_bytes = bytes.fromhex(password_hash)
    except ValueError as error:
        raise AuthFileError(
            f"user {username!r}: password_hash must be hexadecimal"
        ) from error
    if len(password_hash_bytes) != 32:
        raise AuthFileError(
            f"user {username!r}: password_hash must encode exactly 32 bytes"
        )
    raw_salt = value["salt"]
    if not isinstance(raw_salt, list) or not raw_salt:
        raise AuthFileError(f"user {username!r}: salt must be a non-empty byte array")
    if any(type(byte) is not int or not 0 <= byte <= 255 for byte in raw_salt):
        raise AuthFileError(
            f"user {username!r}: every salt value must be an integer from 0 through 255"
        )
    iterations = value["iterations"]
    if type(iterations) is not int or iterations < MINIMUM_ITERATIONS:
        raise AuthFileError(
            f"user {username!r}: iterations must be at least {MINIMUM_ITERATIONS}"
        )
    return {
        "password_hash": password_hash.lower(),
        "salt": list(raw_salt),
        "iterations": iterations,
    }


def _validated_identity(label: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthFileError(f"{label} must be a TOML table")
    allowed = {"subject", "scopes", "tenant_id"}
    extra = set(value) - allowed
    if extra:
        raise AuthFileError(f"{label}: unexpected {', '.join(sorted(extra))}")
    subject = value.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthFileError(f"{label}: subject must be a non-empty string")
    scopes = value.get("scopes", DEFAULT_SCOPES)
    if not isinstance(scopes, list) or any(
        not isinstance(scope, str) or not scope for scope in scopes
    ):
        raise AuthFileError(f"{label}: scopes must be an array of non-empty strings")
    tenant_id = value.get("tenant_id")
    if tenant_id is not None and not isinstance(tenant_id, str):
        raise AuthFileError(f"{label}: tenant_id must be a string")
    result: dict[str, Any] = {"subject": subject, "scopes": sorted(set(scopes))}
    if tenant_id is not None:
        result["tenant_id"] = tenant_id
    return result


def _validated_token(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthFileError(f"token {name!r} must be a TOML table")
    allowed = {"sha256", "subject", "scopes", "tenant_id"}
    extra = set(value) - allowed
    if extra:
        raise AuthFileError(f"token {name!r}: unexpected {', '.join(sorted(extra))}")
    digest = value.get("sha256")
    if not isinstance(digest, str):
        raise AuthFileError(f"token {name!r}: sha256 must be a string")
    try:
        digest_bytes = bytes.fromhex(digest)
    except ValueError as error:
        raise AuthFileError(f"token {name!r}: sha256 must be hexadecimal") from error
    if len(digest_bytes) != 32:
        raise AuthFileError(f"token {name!r}: sha256 must encode exactly 32 bytes")
    identity_fields = {key: item for key, item in value.items() if key != "sha256"}
    identity_fields["subject"] = value.get("subject", name)
    identity = _validated_identity(f"token {name!r}", identity_fields)
    return {"sha256": digest_bytes.hex(), **identity}


def _validated_tls(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthFileError("tls must be a TOML table")
    allowed = {"cert", "key", "client_ca", "client_cert_mode", "identities"}
    extra = set(value) - allowed
    if extra:
        raise AuthFileError(f"tls: unexpected {', '.join(sorted(extra))}")
    for field in ("cert", "key"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise AuthFileError(f"tls.{field} must be a non-empty path string")
    client_ca = value.get("client_ca")
    if client_ca is not None and (not isinstance(client_ca, str) or not client_ca):
        raise AuthFileError("tls.client_ca must be a non-empty path string")
    mode = value.get("client_cert_mode")
    if mode is not None and mode not in {"optional", "required"}:
        raise AuthFileError("tls.client_cert_mode must be optional or required")
    identities = value.get("identities", {})
    if not isinstance(identities, dict):
        raise AuthFileError("tls.identities must be a TOML table")
    if bool(client_ca) != bool(identities):
        raise AuthFileError(
            "tls.client_ca and tls.identities must be configured together"
        )
    if mode is not None and client_ca is None:
        raise AuthFileError("tls.client_cert_mode requires tls.client_ca")
    validated_identities = {}
    for fingerprint, identity in identities.items():
        if not fingerprint.startswith("sha256:"):
            raise AuthFileError("mTLS identity keys must use the sha256:<hex> format")
        try:
            raw_fingerprint = bytes.fromhex(fingerprint.removeprefix("sha256:"))
        except ValueError as error:
            raise AuthFileError(f"invalid mTLS fingerprint {fingerprint!r}") from error
        if len(raw_fingerprint) != 32:
            raise AuthFileError(f"invalid mTLS fingerprint {fingerprint!r}")
        normalized_fingerprint = f"sha256:{raw_fingerprint.hex()}"
        if normalized_fingerprint in validated_identities:
            raise AuthFileError("mTLS fingerprints must be unique after normalization")
        validated_identities[normalized_fingerprint] = _validated_identity(
            f"mTLS identity {fingerprint!r}", identity
        )
    result = {"cert": value["cert"], "key": value["key"]}
    if client_ca is not None:
        result["client_ca"] = client_ca
        result["client_cert_mode"] = mode or "required"
        result["identities"] = validated_identities
    return result


def _validate_static_jwks(jwks: Any, algorithms: list[str]) -> None:
    """Check key shape and the runtime's signing-key selection, not cryptography."""
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
        raise AuthFileError("oidc.jwks requires a nonempty keys array")
    usable_ids: set[str] = set()
    for key in jwks["keys"]:
        if not isinstance(key, dict) or not isinstance(key.get("kty"), str):
            raise AuthFileError("oidc.jwks keys must be objects with a string kty")
        for field in ("kid", "use", "alg", "crv", "x5u"):
            if field in key and not isinstance(key[field], str):
                raise AuthFileError(f"oidc.jwks key {field} must be a string")
        if "key_ops" in key and (
            not isinstance(key["key_ops"], list)
            or any(not isinstance(operation, str) for operation in key["key_ops"])
        ):
            raise AuthFileError("oidc.jwks key_ops must be an array of strings")
        for field in ("n", "e", "x", "y", "d", "p", "q", "dp", "dq", "qi", "k", "x5t", "x5t#S256"):
            if field not in key:
                continue
            value = key[field]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise AuthFileError(f"oidc.jwks key {field} must be unpadded base64url")
            try:
                base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            except (ValueError, binascii.Error) as error:
                raise AuthFileError(f"oidc.jwks key {field} must be unpadded base64url") from error
        kid = key.get("kid")
        if not kid or key.get("use", "sig") != "sig":
            continue
        if "key_ops" in key and "verify" not in key["key_ops"]:
            continue
        algorithm = None
        if key["kty"] == "RSA":
            algorithm = "RS256"
        elif key["kty"] == "EC" and key.get("crv") == "P-256":
            algorithm = "ES256"
        if algorithm not in algorithms or key.get("alg", algorithm) != algorithm:
            continue
        fields = ("n", "e") if algorithm == "RS256" else ("x", "y")
        if any(not key.get(field) for field in fields):
            raise AuthFileError("oidc.jwks signing key is missing public key material")
        if kid in usable_ids:
            raise AuthFileError(f"oidc.jwks has duplicate signing kid {kid}")
        usable_ids.add(kid)
    if not usable_ids:
        raise AuthFileError("oidc.jwks contains no usable signing keys")


def _validated_oidc(providers: Any) -> list[dict[str, Any]]:
    if not isinstance(providers, list):
        raise AuthFileError("oidc must be an array of tables")
    allowed = {
        "name", "issuer", "audience", "audiences", "subject_claims", "jwks",
        "jwks_url", "discovery_url", "allow_http", "algorithms", "groups_claim",
        "tenant_claim", "roles_claim", "access_token_type", "token_use", "required_claims",
    }
    names, issuers = set(), set()
    for provider in providers:
        if not isinstance(provider, dict) or set(provider) - allowed:
            raise AuthFileError("oidc provider has unexpected fields or is not a table")
        name = provider.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise AuthFileError("oidc provider requires a valid name")
        issuer = provider.get("issuer")
        if not isinstance(issuer, str) or not issuer.strip():
            raise AuthFileError("oidc provider requires an issuer")
        if name in names or issuer in issuers:
            raise AuthFileError("oidc provider names and issuers must be unique")
        names.add(name)
        issuers.add(issuer)
        if "allow_http" in provider and type(provider["allow_http"]) is not bool:
            raise AuthFileError("oidc.allow_http must be a boolean")
        for field in ("issuer", "jwks_url", "discovery_url"):
            if field not in provider:
                continue
            value = provider[field]
            if not isinstance(value, str):
                raise AuthFileError(f"oidc.{field} must be a URL")
            try:
                url = urlsplit(value)
            except ValueError as error:
                raise AuthFileError(f"oidc.{field} must be a valid URL") from error
            schemes = {"https", "http"} if provider.get("allow_http", False) else {"https"}
            if url.scheme not in schemes or not url.hostname or url.username or url.password:
                raise AuthFileError(f"oidc.{field} requires an HTTPS URL (HTTP needs allow_http)")
        for field in ("audiences", "subject_claims", "algorithms"):
            if field in provider and (
                not isinstance(provider[field], list)
                or not provider[field]
                or any(not isinstance(item, str) or not item.strip() for item in provider[field])
            ):
                raise AuthFileError(f"oidc.{field} requires nonempty strings")
        for field in ("audience", "groups_claim", "tenant_claim", "roles_claim", "access_token_type", "token_use"):
            if field in provider and (not isinstance(provider[field], str) or not provider[field].strip()):
                raise AuthFileError(f"oidc.{field} must be a nonempty string")
        if not provider.get("audience") and not provider.get("audiences"):
            raise AuthFileError("oidc requires audience or audiences")
        if set(provider.get("algorithms", ["RS256"])) - {"RS256", "ES256"}:
            raise AuthFileError("oidc algorithms must be RS256 or ES256")
        if "jwks" in provider:
            _validate_static_jwks(provider["jwks"], provider.get("algorithms", ["RS256"]))
        if "required_claims" in provider and (
            not isinstance(provider["required_claims"], dict)
            or any(not key.strip() for key in provider["required_claims"])
        ):
            raise AuthFileError("oidc.required_claims must be a table of named claims")
    return providers


def _validated_authorization(value: Any, providers: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"rules"}:
        raise AuthFileError("authorization must contain only rules")
    rules = value.get("rules", [])
    if not isinstance(rules, list):
        raise AuthFileError("authorization.rules must be an array of tables")
    names = {f"oidc:{provider['name']}" for provider in providers}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"provider", "subject", "group", "tenant", "scopes"}:
            raise AuthFileError("authorization rule has unexpected fields")
        if not isinstance(rule.get("provider"), str) or rule["provider"] not in names:
            raise AuthFileError("authorization provider must match a configured oidc:<name>")
        for field in ("subject", "group", "tenant"):
            if field in rule and (not isinstance(rule[field], str) or not rule[field].strip()):
                raise AuthFileError("authorization selectors must be nonempty strings")
        scopes = rule.get("scopes")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(scope not in FULL_ACCESS_SCOPES for scope in scopes)
        ):
            raise AuthFileError("authorization scopes must be supported DuckFlight scopes")
    return {"rules": rules}


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AuthFileError("configuration must contain a TOML document")
    unexpected = set(data) - {"users", "tokens", "tls", "oidc", "authorization", "flight", "shutdown_grace_secs"}
    if unexpected:
        raise AuthFileError(
            f"unexpected top-level field(s): {', '.join(sorted(unexpected))}"
        )
    raw_users = data.get("users", {})
    raw_tokens = data.get("tokens", {})
    if not isinstance(raw_users, dict):
        raise AuthFileError("users must be a TOML table")
    if not isinstance(raw_tokens, dict):
        raise AuthFileError("tokens must be a TOML table")
    users = {name: _validated_user(name, value) for name, value in raw_users.items()}
    counts = {user["iterations"] for user in users.values()}
    if len(counts) > 1:
        rendered = ", ".join(str(value) for value in sorted(counts))
        raise AuthFileError(
            f"configuration contains mixed SCRAM iteration counts: {rendered}"
        )
    tokens = {name: _validated_token(name, value) for name, value in raw_tokens.items()}
    if len({token["sha256"] for token in tokens.values()}) != len(tokens):
        raise AuthFileError("token SHA-256 digests must be unique")
    result: dict[str, Any] = {"users": users, "tokens": tokens}
    if "shutdown_grace_secs" in data:
        result["shutdown_grace_secs"] = _runtime_integer("shutdown_grace_secs", data["shutdown_grace_secs"])
    if "flight" in data:
        flight = data["flight"]
        if not isinstance(flight, dict) or set(flight) - {
            "max_sessions", "session_timeout_ms", "transaction_timeout_ms", "query_timeout_ms",
        }:
            raise AuthFileError("flight contains unsupported runtime options")
        result["flight"] = {
            name: _runtime_integer(f"flight.{name}", value, minimum=1 if name == "max_sessions" else 0)
            for name, value in flight.items()
        }
    if "tls" in data:
        result["tls"] = _validated_tls(data["tls"])
    if "oidc" in data:
        result["oidc"] = _validated_oidc(data["oidc"])
    if "authorization" in data:
        result["authorization"] = _validated_authorization(
            data["authorization"], result.get("oidc", [])
        )
    return result


def _runtime_integer(name: str, value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= (1 << 63) - 1:
        raise AuthFileError(f"{name} must be an integer from {minimum} to {(1 << 63) - 1}")
    return value


def validate_users(data: Any) -> dict[str, dict[str, Any]]:
    """Validate a legacy users-only document."""
    return validate_config(data)["users"]


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"users": {}, "tokens": {}}
    try:
        with path.open("rb") as stream:
            return validate_config(tomllib.load(stream))
    except tomllib.TOMLDecodeError as error:
        raise AuthFileError(f"failed to parse {path}: {error}") from error
    except OSError as error:
        raise AuthFileError(f"failed to read {path}: {error}") from error


def read_users(path: Path) -> dict[str, dict[str, Any]]:
    return read_config(path)["users"]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_identity(lines: list[str], identity: dict[str, Any]) -> None:
    lines.append(f"subject = {_toml_string(identity['subject'])}")
    scopes = ", ".join(_toml_string(scope) for scope in identity["scopes"])
    lines.append(f"scopes = [{scopes}]")
    if "tenant_id" in identity:
        lines.append(f"tenant_id = {_toml_string(identity['tenant_id'])}")


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        fields = (
            f"{_toml_string(key)} = {_toml_value(item)}" for key, item in value.items()
        )
        return "{ " + ", ".join(fields) + " }"
    raise AuthFileError("OIDC claims and keys must contain TOML-compatible values")


def render_config(config: dict[str, Any]) -> str:
    lines = [
        "# Generated by scripts/duckflight_auth.py.",
        "# Contains password verifiers and authentication policy; keep mode 0600.",
    ]
    if "shutdown_grace_secs" in config:
        lines.extend(["", f"shutdown_grace_secs = {config['shutdown_grace_secs']}"])
    if "flight" in config:
        lines.extend(["", "[flight]"])
        lines.extend(f"{name} = {value}" for name, value in config["flight"].items())
    for username in sorted(config["users"]):
        user = config["users"][username]
        salt = ", ".join(str(byte) for byte in user["salt"])
        lines.extend(
            [
                "",
                f"[users.{_toml_string(username)}]",
                f'password_hash = "{user["password_hash"]}"',
                f"salt = [{salt}]",
                f"iterations = {user['iterations']}",
            ]
        )
    for name in sorted(config["tokens"]):
        token = config["tokens"][name]
        lines.extend(
            ["", f"[tokens.{_toml_string(name)}]", f'sha256 = "{token["sha256"]}"']
        )
        _render_identity(lines, token)
    tls = config.get("tls")
    if tls is not None:
        lines.extend(
            [
                "",
                "[tls]",
                f"cert = {_toml_string(tls['cert'])}",
                f"key = {_toml_string(tls['key'])}",
            ]
        )
        if "client_ca" in tls:
            lines.append(f"client_ca = {_toml_string(tls['client_ca'])}")
            lines.append(f"client_cert_mode = {_toml_string(tls['client_cert_mode'])}")
        for fingerprint in sorted(tls.get("identities", {})):
            lines.extend(["", f"[tls.identities.{_toml_string(fingerprint)}]"])
            _render_identity(lines, tls["identities"][fingerprint])
    for provider in config.get("oidc", []):
        lines.extend(["", "[[oidc]]"])
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in provider.items())
    for rule in config.get("authorization", {}).get("rules", []):
        lines.extend(["", "[[authorization.rules]]"])
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in rule.items())
    return "\n".join(lines) + "\n"


def write_config(path: Path, config: dict[str, Any]) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise AuthFileError(f"configuration directory does not exist: {parent}")
    rendered = render_config(validate_config(config))
    temporary_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temporary_path = Path(name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as error:
        raise AuthFileError(f"failed to write {path}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_users(path: Path, users: dict[str, dict[str, Any]]) -> None:
    config = read_config(path)
    config["users"] = users
    write_config(path, config)


def _password(prompt: str, *, confirm: bool) -> str:
    password = getpass.getpass(prompt)
    if not password:
        raise AuthFileError("password must not be empty")
    if confirm and not hmac.compare_digest(
        password, getpass.getpass("Confirm password: ")
    ):
        raise AuthFileError("password confirmation did not match")
    return password


def _environment_iterations() -> int | None:
    value = os.environ.get("DUCKFLIGHT_SCRAM_ITERATIONS")
    if value is None:
        return None
    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise AuthFileError("DUCKFLIGHT_SCRAM_ITERATIONS must be an integer") from error
    return max(parsed, MINIMUM_ITERATIONS)


def _add_user(args: argparse.Namespace) -> int:
    path = Path(args.file)
    users = read_users(path)
    existed = args.username in users
    if existed and not args.replace:
        raise AuthFileError(
            f"user {args.username!r} already exists; pass --replace to rotate it"
        )
    existing_counts = {user["iterations"] for user in users.values()}
    existing_iterations = next(iter(existing_counts), None)
    environment_iterations = _environment_iterations()
    if args.iterations is not None and args.iterations < MINIMUM_ITERATIONS:
        raise AuthFileError(f"iterations must be at least {MINIMUM_ITERATIONS}")
    if (
        args.iterations is not None
        and environment_iterations is not None
        and args.iterations != environment_iterations
    ):
        raise AuthFileError("--iterations does not match DUCKFLIGHT_SCRAM_ITERATIONS")
    iterations = (
        args.iterations
        or environment_iterations
        or existing_iterations
        or DEFAULT_ITERATIONS
    )
    if existing_iterations is not None and existing_iterations != iterations:
        raise AuthFileError(
            f"existing users use {existing_iterations} iterations; new users must match"
        )
    password = _password(f"Password for {args.username}: ", confirm=True)
    salt = secrets.token_bytes(DEFAULT_SALT_BYTES)
    users[args.username] = {
        "password_hash": derive_password_hash(password, salt, iterations),
        "salt": list(salt),
        "iterations": iterations,
    }
    write_users(path, users)
    print(f"{'replaced' if existed else 'added'} {args.username!r} in {path}")
    return 0


def _list_users(args: argparse.Namespace) -> int:
    users = read_users(Path(args.file))
    for username in sorted(users):
        print(f"{username}\titerations={users[username]['iterations']}")
    return 0


def _test_user(args: argparse.Namespace) -> int:
    users = read_users(Path(args.file))
    user = users.get(args.username)
    password = _password(f"Password for {args.username}: ", confirm=False)
    if user is None:
        print("authentication failed", file=sys.stderr)
        return 1
    actual = derive_password_hash(password, bytes(user["salt"]), user["iterations"])
    if not hmac.compare_digest(actual, user["password_hash"]):
        print("authentication failed", file=sys.stderr)
        return 1
    print("authentication succeeded")
    return 0


def _remove_user(args: argparse.Namespace) -> int:
    path = Path(args.file)
    users = read_users(path)
    if args.username not in users:
        raise AuthFileError(f"user {args.username!r} does not exist in {path}")
    del users[args.username]
    write_users(path, users)
    print(f"removed {args.username!r} from {path}")
    return 0


def _add_token(args: argparse.Namespace) -> int:
    path = Path(args.file)
    config = read_config(path)
    existed = args.name in config["tokens"]
    if existed and not args.replace:
        raise AuthFileError(
            f"token {args.name!r} already exists; pass --replace to rotate it"
        )
    raw = secrets.token_urlsafe(32)
    config["tokens"][args.name] = {
        "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "subject": args.subject or args.name,
        "scopes": sorted(
            set(
                FULL_ACCESS_SCOPES if args.full_access else args.scope or DEFAULT_SCOPES
            )
        ),
    }
    write_config(path, config)
    print(f"{'replaced' if existed else 'added'} token {args.name!r} in {path}")
    print(f"token={raw}")
    return 0


def _list_tokens(args: argparse.Namespace) -> int:
    tokens = read_config(Path(args.file))["tokens"]
    for name in sorted(tokens):
        token = tokens[name]
        print(f"{name}\tsubject={token['subject']}\tscopes={','.join(token['scopes'])}")
    return 0


def _test_token(args: argparse.Namespace) -> int:
    token = read_config(Path(args.file))["tokens"].get(args.name)
    supplied = getpass.getpass(f"Token for {args.name}: ")
    actual = hashlib.sha256(supplied.encode()).hexdigest()
    if token is None or not hmac.compare_digest(actual, token["sha256"]):
        print("authentication failed", file=sys.stderr)
        return 1
    print("authentication succeeded")
    return 0


def _remove_token(args: argparse.Namespace) -> int:
    path = Path(args.file)
    config = read_config(path)
    if args.name not in config["tokens"]:
        raise AuthFileError(f"token {args.name!r} does not exist in {path}")
    del config["tokens"][args.name]
    write_config(path, config)
    print(f"removed token {args.name!r} from {path}")
    return 0


def _check_tls(path: Path, config: dict[str, Any]) -> None:
    if not path.exists():
        raise AuthFileError(f"configuration does not exist: {path}")
    tls = config.get("tls")
    if tls is None:
        raise AuthFileError(f"{path}: no tls section configured")
    base = path.parent
    cert = Path(tls["cert"])
    key = Path(tls["key"])
    cert = cert if cert.is_absolute() else base / cert
    key = key if key.is_absolute() else base / key
    _check_permissions(key)
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        if "client_ca" in tls:
            client_ca = Path(tls["client_ca"])
            client_ca = client_ca if client_ca.is_absolute() else base / client_ca
            context.load_verify_locations(client_ca)
    except (OSError, ssl.SSLError) as error:
        raise AuthFileError(f"TLS configuration is invalid: {error}") from error


def _check_permissions(path: Path) -> None:
    if os.name == "posix" and path.exists() and path.stat().st_mode & 0o077:
        raise AuthFileError(
            f"{path} must not be accessible by group or other users; use mode 0600"
        )


def _check_config(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        raise AuthFileError(f"configuration does not exist: {path}")
    config = read_config(path)
    _check_permissions(path)
    if "tls" in config:
        _check_tls(path, config)
    print(
        f"{path}: structurally valid; users={len(config['users'])}; "
        f"tokens={len(config['tokens'])}; oidc={len(config.get('oidc', []))}; "
        f"tls={'yes' if 'tls' in config else 'no'}"
    )
    if config.get("oidc"):
        print("OIDC key cryptography and provider availability are verified by the runtime.")
    return 0


def _check_tls_command(args: argparse.Namespace) -> int:
    path = Path(args.file)
    config = read_config(path)
    _check_tls(path, config)
    print(f"{path}: TLS certificate, key, and client CA (if configured) are valid")
    return 0


def _add_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--file",
        "-f",
        default=DEFAULT_FILE,
        help=f"config file (default: {DEFAULT_FILE})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    user = commands.add_parser("user", help="manage SCRAM users")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    add = user_commands.add_parser("add", help="add a SCRAM user")
    add.add_argument("username")
    _add_file_argument(add)
    add.add_argument("--iterations", type=int)
    add.add_argument("--replace", action="store_true", help="replace an existing user")
    add.set_defaults(handler=_add_user)
    listing = user_commands.add_parser("list", help="list usernames without verifiers")
    _add_file_argument(listing)
    listing.set_defaults(handler=_list_users)
    test = user_commands.add_parser("test", help="verify a user's password")
    test.add_argument("username")
    _add_file_argument(test)
    test.set_defaults(handler=_test_user)
    remove = user_commands.add_parser("remove", help="remove a user")
    remove.add_argument("username")
    _add_file_argument(remove)
    remove.set_defaults(handler=_remove_user)

    token = commands.add_parser("token", help="manage Airport/direct bearer tokens")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    token_add = token_commands.add_parser("add", help="generate and add a bearer token")
    token_add.add_argument("name")
    _add_file_argument(token_add)
    token_add.add_argument("--subject")
    token_scope = token_add.add_mutually_exclusive_group()
    token_scope.add_argument("--scope", action="append")
    token_scope.add_argument(
        "--full-access",
        action="store_true",
        help="grant query writes, ingestion, transactions, and administration",
    )
    token_add.add_argument(
        "--replace", action="store_true", help="rotate an existing token"
    )
    token_add.set_defaults(handler=_add_token)
    token_list = token_commands.add_parser(
        "list", help="list token names without secrets"
    )
    _add_file_argument(token_list)
    token_list.set_defaults(handler=_list_tokens)
    token_test = token_commands.add_parser("test", help="verify a bearer token")
    token_test.add_argument("name")
    _add_file_argument(token_test)
    token_test.set_defaults(handler=_test_token)
    token_remove = token_commands.add_parser("remove", help="remove a bearer token")
    token_remove.add_argument("name")
    _add_file_argument(token_remove)
    token_remove.set_defaults(handler=_remove_token)

    check = commands.add_parser("check", help="validate the complete configuration")
    _add_file_argument(check)
    check.set_defaults(handler=_check_config)
    tls = commands.add_parser("tls", help="validate the TLS certificate and key")
    _add_file_argument(tls)
    tls.set_defaults(handler=_check_tls_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except AuthFileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

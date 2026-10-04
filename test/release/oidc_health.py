#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.6", "pyarrow", "PyJWT[crypto]>=2.10,<3", "grpcio-health-checking"]
# ///
"""Exercise OIDC/JWKS and standard health against a newly bundled core payload."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb
import grpc
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf.any_pb2 import Any as ProtobufAny
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pyarrow import flight

SERVICE = "arrow.flight.protocol.FlightService"


def query(client: flight.FlightClient, token: str) -> list[dict]:
    sql = b"select 42 as answer"
    # CommandStatementQuery's query field is field 1, length-delimited.
    command = ProtobufAny(
        type_url="type.googleapis.com/arrow.flight.protocol.sql.CommandStatementQuery",
        value=b"\x0a" + bytes([len(sql)]) + sql,
    )
    options = flight.FlightCallOptions(
        headers=[(b"authorization", f"Bearer {token}".encode())], timeout=5
    )
    info = client.get_flight_info(
        flight.FlightDescriptor.for_command(command.SerializeToString()), options
    )
    return client.do_get(info.endpoints[0].ticket, options).read_all().to_pylist()


def run(extension: Path) -> None:
    # Standalone compatibility flags must never weaken an explicit extension config.
    os.environ["DUCKFLIGHT_FLIGHT_ALLOW_LEGACY_UNAUTHENTICATED"] = "1"
    key = ec.generate_private_key(ec.SECP256R1())
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    public.update(kid="test", alg="ES256", use="sig")
    jwks = json.dumps({"keys": [public]}).encode()

    class Provider(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(jwks)))
            self.end_headers()
            self.wfile.write(jwks)

        def log_message(self, *_args: object) -> None:
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    serving = threading.Thread(target=provider.serve_forever, daemon=True)
    serving.start()
    issuer = f"http://127.0.0.1:{provider.server_port}"

    def token(group: str, audience: str = "flight") -> str:
        return jwt.encode(
            {"iss": issuer, "aud": audience, "sub": "alice", "groups": [group],
             "scope": "query:admin query:execute", "exp": int(time.time()) + 300},
            key, algorithm="ES256", headers={"kid": "test"},
        )

    try:
        with tempfile.TemporaryDirectory() as directory, duckdb.connect(
            config={"allow_unsigned_extensions": "true"}
        ) as host:
            config = Path(directory) / "auth.toml"
            config.write_text(f'''[[oidc]]
name = "test"
issuer = "{issuer}"
audience = "flight"
jwks_url = "{issuer}/keys"
allow_http = true
algorithms = ["ES256"]
[[authorization.rules]]
provider = "oidc:test"
group = "readers"
scopes = ["query:execute"]
''')
            config.chmod(0o600)
            host.execute("load '" + str(extension.resolve()).replace("'", "''") + "'")
            address = host.execute(
                "select address from duckflight_flight_serve('127.0.0.1:0', ?)",
                [str(config)],
            ).fetchone()[0]
            with flight.FlightClient(f"grpc://{address}") as client:
                try:
                    list(client.list_flights(options=flight.FlightCallOptions(timeout=5)))
                except flight.FlightUnauthenticatedError:
                    pass
                else:
                    raise AssertionError("legacy environment flag disabled extension authentication")
                assert query(client, token("readers")) == [{"answer": 42}]
                for bearer in (token("outsiders"), token("readers", "wrong")):
                    try:
                        query(client, bearer)
                    except (flight.FlightUnauthorizedError, flight.FlightUnauthenticatedError):
                        pass
                    else:
                        raise AssertionError("invalid credentials/policy unexpectedly allowed query")
            with grpc.insecure_channel(address) as channel, ThreadPoolExecutor(1) as executor:
                health = health_pb2_grpc.HealthStub(channel)
                for service in ("", SERVICE):
                    result = health.Check(health_pb2.HealthCheckRequest(service=service), timeout=5)
                    assert result.status == health_pb2.HealthCheckResponse.SERVING
                watch = health.Watch(health_pb2.HealthCheckRequest(service=SERVICE), timeout=10)
                assert next(watch).status == health_pb2.HealthCheckResponse.SERVING

                def observe_stop() -> None:
                    try:
                        assert next(watch).status == health_pb2.HealthCheckResponse.NOT_SERVING
                    finally:
                        watch.cancel()

                stopped = executor.submit(observe_stop)
                host.execute("select * from duckflight_stop('flight', ?)", [address]).fetchall()
                stopped.result(timeout=5)
            print("PASS signed JWKS access, policy denial, wrong audience, health Check/Watch lifecycle")
    finally:
        provider.shutdown()
        provider.server_close()
        serving.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extension", type=Path)
    run(parser.parse_args().extension)

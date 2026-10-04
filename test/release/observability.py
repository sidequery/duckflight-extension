# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.6", "psycopg[binary]", "adbc-driver-flightsql", "pyarrow", "opentelemetry-proto"]
# ///
"""Verify instance-owned exporters from a loaded extension using real clients."""

import hashlib
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import adbc_driver_flightsql.dbapi
import duckdb
import psycopg
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)


def attributes(items):
    return {item.key: item.value for item in items}


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: observability.py EXTENSION")
    config_directory = tempfile.TemporaryDirectory(prefix="duckflight-otlp-")
    config_path = Path(config_directory.name) / "auth.toml"
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", b"testpass", salt, 10_000).hex()
    config_path.write_text(
        f'[users.testuser]\npassword_hash = "{digest}"\nsalt = {list(salt)}\niterations = 10000\n'
    )
    config_path.chmod(0o600)
    requests = []
    lock = threading.Lock()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                requests.append((self.path, payload))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ.update(
        DUCKFLIGHT_TELEMETRY_ENABLED="true",
        DUCKFLIGHT_TELEMETRY_TRACES="true",
        DUCKFLIGHT_TELEMETRY_METRICS="true",
        DUCKFLIGHT_TELEMETRY_LOGS="true",
        DUCKFLIGHT_SAMPLE_RATIO="1",
        DUCKFLIGHT_TELEMETRY_LOG_LEVEL="info",
        DUCKFLIGHT_OTLP_PROTOCOL="http/protobuf",
        DUCKFLIGHT_OTLP_ENDPOINT=f"http://127.0.0.1:{server.server_port}",
        DUCKFLIGHT_SERVICE_NAME="extension-observability-test",
        DUCKFLIGHT_METRICS_INTERVAL_MS="60000",
        DUCKFLIGHT_PROFILE_MODE="detailed",
        DUCKFLIGHT_PROFILE_SAMPLE_RATIO="1",
    )
    host = duckdb.connect(config={"allow_unsigned_extensions": "true"})
    addresses = {}
    try:
        host.execute("create table host_visible as select * from range(17)")
        before = host.execute("select current_setting('enable_profiling')").fetchone()
        extension = str(Path(sys.argv[1]).resolve()).replace("'", "''")
        host.execute(f"load '{extension}'")
        addresses["pgwire"] = host.execute(
            "select address from duckflight_pg_serve('127.0.0.1:0', ?)",
            [str(config_path)],
        ).fetchone()[0]
        addresses["flight"] = host.execute(
            "select address from duckflight_flight_serve('127.0.0.1:0', ?)",
            [str(config_path)],
        ).fetchone()[0]
        pg_host, pg_port = addresses["pgwire"].rsplit(":", 1)
        with psycopg.connect(
            host=pg_host,
            port=int(pg_port),
            user="testuser",
            password="testpass",
            dbname="duckflight",
            autocommit=True,
        ) as pg:
            assert pg.execute("select sum(range) from host_visible").fetchone() == (
                136,
            )
            try:
                pg.execute("select missing_observability_function()")
                raise AssertionError("expected an engine error")
            except psycopg.Error:
                pass
            assert pg.execute("select count(*) from host_visible").fetchone() == (17,)
        with (
            adbc_driver_flightsql.dbapi.connect(
                f"flightsql://{addresses['flight']}?transport=tcp",
                db_kwargs={"username": "testuser", "password": "testpass"},
                autocommit=True,
            ) as flight,
            flight.cursor() as cursor,
        ):
            cursor.execute("select sum(range) from host_visible")
            # Consume EOF: closing after fetchone legitimately abandons a
            # streaming ADBC result before its successful profile is ready.
            assert cursor.fetchall() == [(136,)]
        assert (
            host.execute("select current_setting('enable_profiling')").fetchone()
            == before
        )
        for protocol, address in addresses.items():
            host.execute(
                "select * from duckflight_stop(?, ?)", [protocol, address]
            ).fetchall()
        addresses.clear()

        # A stopped service flushes but does not shut down the shared providers.
        addresses["flight"] = host.execute(
            "select address from duckflight_flight_serve('127.0.0.1:0', ?)",
            [str(config_path)],
        ).fetchone()[0]
        with (
            adbc_driver_flightsql.dbapi.connect(
                f"flightsql://{addresses['flight']}?transport=tcp",
                db_kwargs={"username": "testuser", "password": "testpass"},
                autocommit=True,
            ) as flight,
            flight.cursor() as cursor,
        ):
            cursor.execute("select count(*) from host_visible")
            assert cursor.fetchall() == [(17,)]
        host.execute(
            "select * from duckflight_stop('flight', ?)", [addresses.pop("flight")]
        ).fetchall()
    finally:
        for protocol, address in addresses.items():
            host.execute(
                "select * from duckflight_stop(?, ?)", [protocol, address]
            ).fetchall()
        host.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        config_directory.cleanup()

    spans, logs, metrics, resources = [], [], [], []
    for path, payload in requests:
        if path == "/v1/traces":
            request = ExportTraceServiceRequest.FromString(payload)
            for resource in request.resource_spans:
                resources.append(attributes(resource.resource.attributes))
                spans.extend(
                    span for scope in resource.scope_spans for span in scope.spans
                )
        elif path == "/v1/logs":
            request = ExportLogsServiceRequest.FromString(payload)
            logs.extend(
                record
                for resource in request.resource_logs
                for scope in resource.scope_logs
                for record in scope.log_records
            )
        elif path == "/v1/metrics":
            request = ExportMetricsServiceRequest.FromString(payload)
            metrics.extend(
                metric
                for resource in request.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            )
    queries = [span for span in spans if span.name == "duckflight.query"]
    assert queries and logs and metrics, "all three OTLP signals must export"
    assert len(queries) == 5, f"expected one span per query, got {len(queries)}"
    assert len(logs) == 5, f"expected one completion log per query, got {len(logs)}"
    protocols = {
        attributes(span.attributes)["network.protocol.name"].string_value
        for span in queries
    }
    assert {"pgwire", "flight"} <= protocols, protocols
    assert all(
        resource["duckflight.surface"].string_value == "extension"
        for resource in resources
    )
    assert all(
        resource["service.name"].string_value == "extension-observability-test"
        for resource in resources
    )
    assert any(
        attributes(span.attributes)["duckflight.outcome"].string_value == "error"
        for span in queries
    )
    profiled = {
        attributes(span.attributes)["network.protocol.name"].string_value
        for span in queries
        if any(event.name == "duckdb.profile" for event in span.events)
    }
    assert {"pgwire", "flight"} <= profiled, profiled
    assert all(
        attributes(span.attributes)["duckflight.attempts"].int_value == 1
        for span in queries
    )
    span_ids = {span.span_id for span in queries}
    assert any(record.span_id in span_ids and record.trace_id for record in logs)
    assert any(metric.name == "duckflight.queries.total" for metric in metrics)
    print(
        f"Extension OTLP verified: {len(queries)} queries, {len(logs)} logs, both protocols profiled"
    )


if __name__ == "__main__":
    main()

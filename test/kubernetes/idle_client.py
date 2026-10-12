"""Real-client sleep/wake and activity protection, inside the disposable cluster."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

import psycopg
from docker_smoke import flight_execute, flight_query, protobuf_strings
from pyarrow import flight

HOST = "lake-duckflight"
CA = Path("/run/test/server.crt")
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def replicas() -> int:
    namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    if ":" in host:
        host = f"[{host}]"
    url = f"https://{host}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}/apis/apps/v1/namespaces/{namespace}/deployments/{HOST}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + (SERVICE_ACCOUNT / "token").read_text().strip()
        },
    )
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    with urllib.request.urlopen(request, context=context, timeout=5) as response:
        return json.load(response)["spec"]["replicas"]


def remains_awake(seconds: int = 25) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert replicas() == 1, "idle controller killed protected activity"
        time.sleep(1)


def sleeps() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if replicas() == 0:
            return
        time.sleep(1)
    raise AssertionError("worker did not sleep after sessions/results were closed")


def postgres():
    return psycopg.connect(
        host=HOST,
        port=5433,
        user="e2e",
        password=os.environ["CLIENT_PASSWORD"],
        dbname="duckflight",
        sslmode="verify-full",
        sslrootcert=str(CA),
        connect_timeout=120,
        autocommit=True,
    )


def flight_client():
    return flight.FlightClient(
        f"grpc+tls://{HOST}:31337", tls_root_certs=CA.read_bytes()
    )


def flight_info(client, options, query):
    command = protobuf_strings(
        {
            1: b"type.googleapis.com/arrow.flight.protocol.sql.CommandStatementQuery",
            2: protobuf_strings({1: query.encode()}),
        }
    )
    return client.get_flight_info(flight.FlightDescriptor.for_command(command), options)


def main() -> None:
    assert replicas() == 0, "idle installation must start asleep"
    with postgres() as pg:
        assert pg.execute("select 42").fetchone() == (42,)
        pg.execute("create temp table retained as select 17 as value")
        remains_awake()
        assert pg.execute("select value from retained").fetchone() == (17,)
        with pg.cursor() as cursor:
            # Exceed socket buffers without exhausting a 1GiB test worker.
            stream = cursor.stream("select range from range(1000000)")
            assert next(stream) == (0,)
            remains_awake()
            assert next(stream) == (1,)
            pg.close()  # Explicit client disconnect ends the pending result.
            stream.close()
    sleeps()
    print(
        "PASS PostgreSQL: cold wake, idle session and active result survive, then sleep",
        flush=True,
    )

    with flight_client() as client:
        header = client.authenticate_basic_token(
            "e2e", os.environ["CLIENT_PASSWORD"], flight.FlightCallOptions(timeout=120)
        )
        options = flight.FlightCallOptions(headers=[header], timeout=120)
        flight_execute(
            client, options, "create temp table retained as select 23 as value"
        )
        pending = flight_info(client, options, "select value from retained")
    # Closing a channel does not close the Basic-handshake session or its ticket.
    remains_awake()
    with flight_client() as client:
        assert client.do_get(
            pending.endpoints[0].ticket, options
        ).read_all().to_pylist() == [{"value": 23}]
        assert flight_query(client, options, "select value from retained") == [
            {"value": 23}
        ]
        info = flight_info(client, options, "select range from range(1000000)")
        reader = client.do_get(info.endpoints[0].ticket, options)
        assert reader.read_chunk().data.num_rows > 0
        remains_awake()
        assert reader.read_chunk().data.num_rows > 0
        reader.cancel()
        # A reader retains its transport even after cancellation.
        del reader
        list(client.do_action(flight.Action("CloseSession", b""), options))
    sleeps()
    print(
        "PASS Flight: cold wake, detached session/ticket and active stream survive, CloseSession allows sleep",
        flush=True,
    )

    with postgres() as pg:
        assert pg.execute("select 77").fetchone() == (77,)
    sleeps()
    print(
        "PASS repeated wake after zero workers; idle probes did not prevent sleep",
        flush=True,
    )

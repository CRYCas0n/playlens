"""SSE over a real socket.

The in-process ASGI transport cannot terminate an infinite generator mid-iteration, so
the earlier attempt at this hung and was replaced by tests of the replay source alone.
This runs a real uvicorn server in a thread and speaks HTTP to it, which is the only way
to see the actual wire format: the ``id:`` / ``event:`` / ``data:`` framing, and
``Last-Event-ID`` arriving as a header rather than as a function argument.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

STARTUP_TIMEOUT_S = 20
READ_TIMEOUT_S = 15


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """A real uvicorn server on a real port, with a database of its own."""
    import uvicorn
    from alembic import command
    from alembic.config import Config

    from app.config import reset_settings_cache
    from app.container import reset_container
    from app.main import create_app

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    db_path = tmp_path_factory.mktemp("sse") / "sse.db"
    url = f"sqlite+pysqlite:///{db_path}"

    previous = {k: os.environ.get(k) for k in ("DATABASE_URL", "ADMIN_TOKEN", "APP_ENV")}
    os.environ["DATABASE_URL"] = url
    os.environ["ADMIN_TOKEN"] = "s" * 32
    os.environ["APP_ENV"] = "test"
    reset_settings_cache()
    reset_container()

    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    command.upgrade(cfg, "head")

    port = free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + STARTUP_TIMEOUT_S
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.skip("uvicorn did not start in time")

    yield f"http://127.0.0.1:{port}", url

    server.should_exit = True
    thread.join(timeout=10)
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_cache()
    reset_container()


def emit(url: str, event: str, message: str) -> int:
    """Write an event straight into the log the stream reads from."""
    import sqlalchemy as sa

    from app.db.base import make_session_factory
    from app.db.uow import UnitOfWork

    engine = sa.create_engine(url)
    try:
        with UnitOfWork(make_session_factory(engine)) as uow:
            return int(uow.events.emit(event, message=message))
    finally:
        engine.dispose()


def _parse(block: str) -> dict[str, str] | None:
    if not block.strip():
        return None
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if line.startswith(":"):
            fields["comment"] = line[1:].strip()
            continue
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


class Stream:
    """One reader over one response.

    httpx allows a response body to be iterated exactly once, so a test that reads the
    opening snapshot and then waits for a live event has to keep the same iterator and
    the same partial-frame buffer between the two reads.
    """

    def __init__(self, response) -> None:
        self._chunks = response.iter_text()
        self._buffer = ""

    def read(
        self, *, want: int, timeout_s: float = READ_TIMEOUT_S, comments: bool = False
    ) -> list[dict]:
        """Frames off the wire. Heartbeat comments are skipped unless asked for.

        The keep-alive ``: ping`` is a legal frame with no event and no data. Counting it
        as a delivered event would make every timing-sensitive assertion here flaky.
        """
        frames: list[dict] = []
        deadline = time.time() + timeout_s
        while True:
            while "\n\n" in self._buffer:
                block, self._buffer = self._buffer.split("\n\n", 1)
                frame = _parse(block)
                if frame is None:
                    continue
                if not comments and set(frame) == {"comment"}:
                    continue
                frames.append(frame)
                if len(frames) >= want:
                    return frames
            if time.time() > deadline:
                return frames
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                return frames


class TestTheHandshake:
    def test_the_response_is_an_event_stream(self, live_server):
        base, _ = live_server
        with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
            "GET", f"{base}/api/v1/monitoring/stream"
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store"
            # Nginx buffers an event stream into uselessness without this header.
            assert response.headers["x-accel-buffering"] == "no"
            assert Stream(response).read(want=1)

    def test_it_opens_with_a_snapshot_carrying_an_id(self, live_server):
        base, _ = live_server
        with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
            "GET", f"{base}/api/v1/monitoring/stream"
        ) as response:
            frames = Stream(response).read(want=1)

        first = frames[0]
        assert first["event"] == "snapshot"
        assert "id" in first, "without an id the client has nothing to reconnect from"
        payload = json.loads(first["data"])
        assert {"system", "queues", "stages"} <= set(payload)


class TestDelivery:
    def test_an_event_written_after_connecting_arrives(self, live_server):
        base, url = live_server
        with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
            "GET", f"{base}/api/v1/monitoring/stream"
        ) as response:
            stream = Stream(response)
            stream.read(want=1)  # the opening snapshot
            emit(url, "run.started", "live one")
            frames = stream.read(want=1)

        assert frames, "nothing arrived on the stream"
        assert frames[0]["event"] == "run.started"
        assert json.loads(frames[0]["data"])["message"] == "live one"

    def test_events_arrive_in_order_with_ascending_ids(self, live_server):
        base, url = live_server
        with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
            "GET", f"{base}/api/v1/monitoring/stream"
        ) as response:
            stream = Stream(response)
            stream.read(want=1)
            for index in range(3):
                emit(url, "run.started", f"ordered {index}")
            frames = stream.read(want=3)

        assert [json.loads(f["data"])["message"] for f in frames] == [
            "ordered 0",
            "ordered 1",
            "ordered 2",
        ]
        ids = [int(f["id"]) for f in frames]
        assert ids == sorted(ids)


class TestReconnect:
    def test_last_event_id_replays_what_was_missed(self, live_server):
        """The property SSE was chosen for: a client away for a minute misses nothing."""
        base, url = live_server
        first = emit(url, "run.started", "missed one")
        emit(url, "run.started", "missed two")

        with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
            "GET",
            f"{base}/api/v1/monitoring/stream",
            headers={"Last-Event-ID": str(first - 1)},
        ) as response:
            frames = Stream(response).read(want=3)

        assert frames[0]["event"] == "snapshot"
        messages = [
            json.loads(f["data"]).get("message")
            for f in frames
            if f.get("event") == "run.started"
        ]
        assert "missed one" in messages
        assert "missed two" in messages

    def test_a_nonsense_last_event_id_is_tolerated(self, live_server):
        """A client can send anything; the stream must open rather than fail."""
        base, _ = live_server
        with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
            "GET",
            f"{base}/api/v1/monitoring/stream",
            headers={"Last-Event-ID": "not-a-number"},
        ) as response:
            assert response.status_code == 200
            frames = Stream(response).read(want=1)
        assert frames[0]["event"] == "snapshot"


class TestDisconnect:
    def test_the_server_survives_a_client_hanging_up(self, live_server):
        """The generator has to notice and stop; otherwise every refresh leaks one."""
        base, _ = live_server
        for _ in range(3):
            with httpx.Client(timeout=READ_TIMEOUT_S) as client, client.stream(
                "GET", f"{base}/api/v1/monitoring/stream"
            ) as response:
                Stream(response).read(want=1)

        with httpx.Client(timeout=READ_TIMEOUT_S) as client:
            assert client.get(f"{base}/api/v1/health").status_code == 200
            with client.stream("GET", f"{base}/api/v1/monitoring/stream") as response:
                assert Stream(response).read(want=1)[0]["event"] == "snapshot"

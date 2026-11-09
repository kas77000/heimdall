"""Sleipnir ↔ Huginn over the local pipe: the real API app behind a real named pipe."""
import asyncio
import random
import threading
from multiprocessing.connection import Client

import pytest

from heimdall import pipe
from heimdall.client import HuginnClient, HuginnError, HuginnUnreachable
from heimdall.demo import seed
from heimdall.huginn.api import create_app
from heimdall.sleipnir.scheduler import Sleipnir


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(5)


@pytest.fixture
def port():
    return random.randint(40000, 59999)   # names the pipe and key only: no TCP port is opened


@pytest.fixture
def served(store, engine, loop, port):
    server = pipe.PipeServer(create_app(store, engine, engine.cfg), port, loop)
    server.start()
    yield server
    server.stop()


def _client(port, token=""):
    return HuginnClient(f"http://127.0.0.1:{port}", token=token, user="sleipnir",
                        transport=pipe.PipeTransport(port))


def test_calls_reach_the_api_and_count_as_sleipnirs_heartbeat(served, store, engine, port):
    seed(store)
    c = _client(port)
    assert engine.sleipnir_seen is None
    assert len(c.get("/schedules")) == 3
    assert engine.sleipnir_seen is not None
    alert = next(a for a in store.alerts() if a.enabled)
    r = c.post(f"/alerts/{alert.id}/disable", detail="by schedule 1")
    assert r["changed"] and not store.alert(alert.id).enabled
    assert store.events()[0]["actor"] == "sleipnir"          # the X-Heimdall-User header got through


def test_api_errors_come_back_as_they_would_over_http(served, port):
    with pytest.raises(HuginnError) as exc:
        _client(port).post("/alerts/999999/run", source="schedule")
    assert exc.value.status == 404


def test_the_token_is_still_checked(store, engine, loop, port, monkeypatch):
    monkeypatch.setenv("HEIMDALL_PIPE_TOKEN", "s3cret")
    engine.cfg.token_env = "HEIMDALL_PIPE_TOKEN"
    server = pipe.PipeServer(create_app(store, engine, engine.cfg), port, loop)
    server.start()
    try:
        with pytest.raises(HuginnError) as exc:
            _client(port).get("/schedules")
        assert exc.value.status == 401
        assert _client(port, token="s3cret").get("/schedules") == []
    finally:
        server.stop()


def test_a_client_without_the_key_gets_nothing(served, port, engine):
    conn = Client(pipe.address(port))                        # no key: it cannot answer the challenge
    received = []
    try:
        conn.send_bytes(b'{"method": "GET", "path": "/schedules", "headers": [], "body": ""}')
        for _ in range(5):                                   # the challenge, the refusal, then hang-up
            try:
                received.append(conn.recv_bytes())
            except (EOFError, OSError):
                break
        else:
            pytest.fail("Huginn kept the connection open")
    finally:
        conn.close()
    assert not any(m.startswith(b"{") for m in received)     # never an API answer
    assert engine.sleipnir_seen is None                      # the call never reached the API


def test_a_wrong_key_is_refused(served, port):
    good = pipe.key_path(port).read_bytes()
    pipe.key_path(port).write_bytes(b"0" * len(good))
    try:
        with pytest.raises(HuginnUnreachable):
            _client(port).get("/schedules")
    finally:
        pipe.key_path(port).write_bytes(good)


def test_huginn_down_then_restarted(store, engine, loop, port):
    c = _client(port)
    with pytest.raises(HuginnUnreachable, match="not running"):
        c.get("/schedules")
    for _ in range(2):                                       # a new key each start; the client follows
        server = pipe.PipeServer(create_app(store, engine, engine.cfg), port, loop)
        server.start()
        try:
            assert c.get("/schedules") == []
        finally:
            server.stop()
    assert not pipe.key_path(port).exists()                  # Huginn cleans up its key


def test_sleipnir_ticks_over_the_pipe(served, store, port):
    seed(store)
    assert Sleipnir(_client(port)).tick() is not None

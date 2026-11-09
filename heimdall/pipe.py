"""Sleipnir ↔ Huginn over a local pipe instead of HTTP.

Sleipnir always runs beside Huginn and never serves people, so it does not need
HTTP or the network: it talks to Huginn through a Windows named pipe
(``\\\\.\\pipe\\heimdall-huginn-<port>``) or, elsewhere, a Unix socket. No TCP
port, no proxy, nothing a firewall or a company proxy can block.

What travels on the pipe is the same API call Sleipnir would have made over
HTTP (method, path, headers, body), as JSON. Huginn hands it to its own API
app, so validation, the token check, History and Sleipnir's heartbeat all
behave exactly as before.

Security: each time Huginn starts it writes a random key to a file in the
user's temp folder (readable by that Windows account only). A client must prove
it knows the key (an HMAC challenge, both ways) before Huginn reads a byte from
it, and nothing on the pipe is unpickled, so a stranger who reaches the pipe
gets nothing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener, answer_challenge, deliver_challenge
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

MAX_MESSAGE = 32 * 1024 * 1024
_DROP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "content-encoding"}


def address(port: int) -> str:
    """The pipe (Windows) or socket path (elsewhere) of the Huginn on this port."""
    if sys.platform == "win32":
        return rf"\\.\pipe\heimdall-huginn-{port}"
    return str(Path(tempfile.gettempdir()) / f"heimdall-huginn-{port}.sock")


def key_path(port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"heimdall-huginn-{port}.key"


def read_key(port: int) -> bytes:
    try:
        return key_path(port).read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"Huginn is not running here (no pipe key at {key_path(port)})") from None


# -- Huginn's side -----------------------------------------------------------------

class PipeServer:
    """Answers Sleipnir's calls by passing them to Huginn's API app, on its own event loop."""

    def __init__(self, app, port: int, loop: asyncio.AbstractEventLoop):
        self.port = port
        self.address = address(port)
        self._loop = loop
        self._api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://huginn-pipe")
        self._key = secrets.token_hex(32).encode()
        self._listener: Optional[Listener] = None
        self._closed = threading.Event()
        self._conns: set = set()                  # open connections, closed by stop()
        self._conns_lock = threading.Lock()

    def start(self) -> None:
        if sys.platform != "win32" and os.path.exists(self.address):
            self._remove_stale_socket()
        # No authkey here: the challenge is done per connection, in its own thread, so a
        # client that never answers cannot hold up the others.
        self._listener = Listener(self.address)
        path = key_path(self.port)
        path.write_bytes(self._key)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
        self._thread = threading.Thread(target=self._accept_loop, name="huginn-pipe", daemon=True)
        self._thread.start()
        log.info("Sleipnir pipe listening at %s", self.address)

    def _remove_stale_socket(self) -> None:
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(self.address)
        except OSError:
            os.unlink(self.address)          # left by a Huginn that did not stop cleanly
        else:
            raise RuntimeError(f"another Huginn already listens at {self.address}")
        finally:
            s.close()

    def stop(self) -> None:
        self._closed.set()
        try:
            if key_path(self.port).read_bytes() == self._key:
                key_path(self.port).unlink()
        except OSError:
            pass
        if self._listener is None:
            return
        # accept() is blocked waiting on a pipe instance it holds, which close() alone would
        # leave open (and the pipe's name taken): wake it with a connection of our own first.
        try:
            Client(self.address).close()
        except OSError:
            pass
        self._thread.join(5)
        try:
            self._listener.close()
        except OSError:
            pass
        with self._conns_lock:
            conns, self._conns = list(self._conns), set()
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                conn = self._listener.accept()
            except (OSError, EOFError):
                if self._closed.is_set():
                    return
                continue
            if self._closed.is_set():
                conn.close()
                return
            threading.Thread(target=self._serve, args=(conn,), name="huginn-pipe-call", daemon=True).start()

    def _serve(self, conn) -> None:
        with self._conns_lock:
            self._conns.add(conn)
        try:
            deliver_challenge(conn, self._key)
            answer_challenge(conn, self._key)
            while True:
                try:
                    raw = conn.recv_bytes(MAX_MESSAGE)
                except EOFError:
                    return
                conn.send_bytes(self._handle(raw))
        except (AuthenticationError, EOFError, OSError) as exc:
            log.debug("pipe client dropped: %s", exc)
        finally:
            with self._conns_lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, raw: bytes) -> bytes:
        try:
            req = json.loads(raw)
            fut = asyncio.run_coroutine_threadsafe(
                self._api.request(req["method"], req["path"], headers=req["headers"],
                                  content=base64.b64decode(req["body"])), self._loop)
            r = fut.result()
            log.debug("pipe %s %s -> %s", req["method"], req["path"], r.status_code)
            out = {"status": r.status_code,
                   "headers": [[k, v] for k, v in r.headers.items() if k.lower() not in _DROP],
                   "body": base64.b64encode(r.content).decode()}
        except Exception as exc:  # noqa: BLE001 - the caller gets a 500, Huginn keeps going
            log.exception("pipe call failed")
            out = {"status": 500, "headers": [["content-type", "application/json"]],
                   "body": base64.b64encode(json.dumps({"detail": f"pipe: {exc}"}).encode()).decode()}
        return json.dumps(out).encode()


# -- Sleipnir's side ---------------------------------------------------------------

class PipeTransport(httpx.BaseTransport):
    """An httpx transport that sends each request to Huginn through its pipe."""

    def __init__(self, port: int):
        self.port = port
        self.address = address(port)
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self):
        key = read_key(self.port)                 # read each time: Huginn makes a new key per start
        conn = Client(self.address)
        answer_challenge(conn, key)
        deliver_challenge(conn, key)
        return conn

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.dumps({
            "method": request.method,
            "path": request.url.raw_path.decode("ascii"),
            "headers": [[k, v] for k, v in request.headers.items() if k.lower() not in _DROP],
            "body": base64.b64encode(request.read()).decode(),
        }).encode()
        with self._lock:
            for attempt in (1, 2):                # a kept connection may have died with Huginn: retry once
                try:
                    if self._conn is None:
                        self._conn = self._connect()
                    self._conn.send_bytes(payload)
                    reply = json.loads(self._conn.recv_bytes(MAX_MESSAGE))
                    break
                except (OSError, EOFError, AuthenticationError) as exc:
                    self._drop()
                    if attempt == 2:
                        raise httpx.ConnectError(f"cannot reach Huginn's pipe {self.address}: {exc}",
                                                 request=request) from None
        return httpx.Response(reply["status"], headers=reply["headers"],
                              content=base64.b64decode(reply["body"]), request=request)

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._drop()

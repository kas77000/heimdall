"""Start Sleipnir:  python -m heimdall.sleipnir [-c heimdall.toml] [--url http://huginn:8765] [--http]

Sleipnir reaches a Huginn on this machine through its local pipe (heimdall.pipe):
no HTTP, no network, nothing a proxy or firewall can block. Only a Huginn on
another machine is reached over HTTP, at ``[sleipnir] url``.
"""
from __future__ import annotations

import argparse
import logging
import socket
from urllib.parse import urlsplit

from heimdall import config as config_mod
from heimdall import pipe
from heimdall.client import HuginnClient
from heimdall.sleipnir.scheduler import Sleipnir


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="sleipnir", description="Heimdall's scheduler")
    p.add_argument("-c", "--config")
    p.add_argument("--url", help="Huginn's URL")
    p.add_argument("--http", action="store_true", help="use HTTP even for a Huginn on this machine")
    p.add_argument("--log-level", default="info")
    a = p.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(a.config)
    url = a.url or cfg.huginn_url
    transport = None
    if not a.http and is_this_machine(url):
        transport = pipe.PipeTransport(urlsplit(url).port or cfg.port)
    client = HuginnClient(url, token=cfg.token, user="sleipnir", transport=transport)
    if transport is not None:
        client.url = f"Huginn's local pipe {transport.address}"   # what logs and errors name
        logging.getLogger("httpx").setLevel(logging.WARNING)      # its "HTTP Request" lines would mislead
    Sleipnir(client, cfg.sleipnir_tick_secs, cfg.sleipnir_run_grace_secs).run_forever()


def is_this_machine(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    names = {socket.gethostname().lower(), socket.getfqdn().lower()}
    return host in names


if __name__ == "__main__":
    main()

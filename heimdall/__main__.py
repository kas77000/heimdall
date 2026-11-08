"""One command for every part.

    python -m heimdall all [--demo]     Huginn, Sleipnir and Bifrost together (Ctrl+C stops all three)
    python -m heimdall huginn [...]     the service (API + engine)
    python -m heimdall sleipnir [...]   the scheduler
    python -m heimdall bifrost [--port 8501]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "heimdall" / "bifrost" / "app.py"


def _bifrost_cmd(port: str) -> list[str]:
    return [sys.executable, "-m", "streamlit", "run", str(APP), "--server.port", port,
            "--server.headless", "true"]


def _wait_for_huginn(url: str, timeout: float = 30.0) -> bool:
    import httpx
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if httpx.get(url.rstrip("/") + "/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def run_all(args: list[str]) -> None:
    from heimdall import config as config_mod
    cfg = config_mod.load()
    port = "8501"
    if "--bifrost-port" in args:
        i = args.index("--bifrost-port")
        port = args[i + 1]
        args = args[:i] + args[i + 2:]
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    procs: list[tuple[str, subprocess.Popen]] = []

    def start(name, cmd):
        log = open(logs / f"{name}.log", "a", encoding="utf-8")
        procs.append((name, subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)))
        print(f"  {name:<9} started, log: {logs / (name + '.log')}")

    print("Heimdall")
    start("huginn", [sys.executable, "-m", "heimdall.huginn", *args])
    if not _wait_for_huginn(cfg.huginn_url):
        print(f"Huginn did not answer at {cfg.huginn_url}; see its log.")
    start("sleipnir", [sys.executable, "-m", "heimdall.sleipnir"])
    start("bifrost", _bifrost_cmd(port))
    print(f"\n  Bifrost:  http://localhost:{port}\n  Huginn:   {cfg.huginn_url}/docs\n\nCtrl+C stops all three.")
    try:
        while True:
            for name, p in procs:
                if p.poll() is not None:
                    print(f"{name} stopped (exit {p.returncode}); stopping the others. See its log.")
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def main() -> None:
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "huginn":
        from heimdall.huginn.__main__ import main as m
        m(rest)
    elif cmd == "sleipnir":
        from heimdall.sleipnir.__main__ import main as m
        m(rest)
    elif cmd == "bifrost":
        port = rest[rest.index("--port") + 1] if "--port" in rest else "8501"
        sys.exit(subprocess.call(_bifrost_cmd(port), cwd=ROOT))
    elif cmd == "all":
        run_all(rest)
    else:
        print(__doc__)
        sys.exit(0 if cmd in ("", "-h", "--help") else 2)


if __name__ == "__main__":
    main()

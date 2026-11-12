"""Start the whole local stack with one command: both mock providers and the gateway.

    python -m tools.dev_stack              # keeps prism.db (usage and cache) between runs
    python -m tools.dev_stack --fresh-db   # deletes prism.db first: a clean slate for a demo or a measurement

Run it from the project folder with the venv active. Stop everything with Ctrl+C. Each process writes to
.prism-logs/<name>.log. It refuses to start if one of the ports is already taken, which is what a gateway
or mock left running from an earlier session looks like (a stale process serves OLD code and OLD config).
"""

import argparse
import http.client
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings

ALPHA_PORT, BETA_PORT = 9001, 9002  # the provider URLs in data/gateway_config.sample.json
LOG_DIR = PROJECT_ROOT / ".prism-logs"


def build_commands(python: str, gateway_port: int = 8080) -> dict[str, list[str]]:
    return {
        "alpha": [python, "scripts/mock_provider.py", "--port", str(ALPHA_PORT), "--name", "alpha"],
        "beta": [python, "scripts/mock_provider.py", "--port", str(BETA_PORT), "--name", "beta"],
        "gateway": [python, "-m", "uvicorn", "app.main:app", "--port", str(gateway_port)],
    }


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_healthy(port: int, proc, timeout_s: float = 20.0) -> bool:
    """True once GET /health answers 200. False if the process dies first or the time runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/health")
            ok = conn.getresponse().status == 200
            conn.close()
            if ok:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def stop_all(procs: dict[str, subprocess.Popen]) -> None:
    for proc in procs.values():
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + 5
    for proc in procs.values():
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            proc.kill()
        log = getattr(proc, "_prism_log", None)
        if log:
            log.close()


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def install_shutdown_handlers() -> None:
    """Make Ctrl+C, a plain `kill` and a closed terminal all run the cleanup that stops the children.

    SIGTERM would otherwise end this process at once and orphan the mocks and the gateway. Ctrl+C is set
    explicitly too: a launcher started as a background job inherits "ignore SIGINT" and would never stop.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)
    for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK exists on Windows only
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _raise_interrupt)


def ensure_ports_free(ports: dict[str, int]) -> None:
    busy = [f"{name} (port {port})" for name, port in ports.items() if port_in_use(port)]
    if busy:
        raise StackError(
            f"already in use: {', '.join(busy)}. A mock or gateway from an earlier session is still running "
            "(it serves OLD code and config). Close its terminal or stop that process, then run this again."
        )


class StackError(RuntimeError):
    """A service failed to start or stopped unexpectedly. The message includes the end of its log."""


def _start(name: str, command: list[str], port: int, env: dict, log_dir: Path) -> subprocess.Popen:
    log_path = log_dir / f"{name}.log"
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 (lives as long as the child; closed in stop_all)
    proc = subprocess.Popen(command, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)
    proc._prism_log = log  # type: ignore[attr-defined]
    if not wait_healthy(port, proc):
        stop_all({name: proc})
        raise StackError(f"{name} did not become healthy. Last lines of {log_path}:\n{tail(log_path)}")
    return proc


def start_stack(gateway_port: int = 8080, db_path: Path | None = None, log_dir: Path = LOG_DIR, announce=print) -> dict:
    """Start alpha, beta and the gateway; return the processes. Raises StackError on failure."""
    ports = {"alpha": ALPHA_PORT, "beta": BETA_PORT, "gateway": gateway_port}
    ensure_ports_free(ports)
    env = {**os.environ, **({"PRISM_DB_PATH": str(db_path)} if db_path else {})}
    log_dir.mkdir(exist_ok=True)
    procs: dict[str, subprocess.Popen] = {}
    try:
        for name, command in build_commands(sys.executable, gateway_port).items():
            procs[name] = _start(name, command, ports[name], env, log_dir)
            announce(f"  up: {name:8} http://127.0.0.1:{ports[name]}")
    except StackError:
        stop_all(procs)
        raise
    return procs


def restart_gateway(
    procs: dict, gateway_port: int = 8080, db_path: Path | None = None, log_dir: Path = LOG_DIR
) -> None:
    """Stop and start only the gateway: the in-memory state (rate limiter, provider health) resets, the
    SQLite database (keys, usage, cache entries) is kept."""
    stop_all({"gateway": procs["gateway"]})
    env = {**os.environ, **({"PRISM_DB_PATH": str(db_path)} if db_path else {})}
    command = build_commands(sys.executable, gateway_port)["gateway"]
    procs["gateway"] = _start("gateway", command, gateway_port, env, log_dir)


def main() -> int:
    install_shutdown_handlers()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fresh-db", action="store_true", help="delete the SQLite database before starting")
    parser.add_argument("--gateway-port", type=int, default=8080)
    args = parser.parse_args()

    db_path = get_settings().db_path
    ports = {"alpha": ALPHA_PORT, "beta": BETA_PORT, "gateway": args.gateway_port}
    try:
        ensure_ports_free(ports)  # check BEFORE deleting anything
    except StackError as err:
        print(f"Cannot start: {err}")
        return 1
    if args.fresh_db:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        print(f"Deleted {db_path} (fresh start).")

    procs: dict[str, subprocess.Popen] = {}
    try:
        procs = start_stack(args.gateway_port)
        print(f"\nPrism is running. Console: http://127.0.0.1:{args.gateway_port}/console")
        print("Admin token: demo-admin-token (unless PRISM_ADMIN_TOKEN is set). Press Ctrl+C to stop everything.\n")
        while True:
            for name, proc in procs.items():
                if proc.poll() is not None:
                    print(
                        f"{name} stopped unexpectedly (exit {proc.returncode}). Last log lines:\n{tail(LOG_DIR / (name + '.log'))}"
                    )
                    return 1
            time.sleep(1)
    except StackError as err:
        print(f"Cannot start: {err}")
        return 1
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    finally:
        stop_all(procs)


if __name__ == "__main__":
    raise SystemExit(main())
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tools import dev_stack


class FakeProc:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


def serve(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_commands_start_both_mocks_and_the_gateway_on_the_ports_the_config_expects():
    commands = dev_stack.build_commands("py", gateway_port=8123)
    assert commands["alpha"][:2] == ["py", "scripts/mock_provider.py"] and "9001" in commands["alpha"]
    assert commands["beta"][-1] == "beta" and "9002" in commands["beta"]
    assert commands["gateway"] == ["py", "-m", "uvicorn", "app.main:app", "--port", "8123"]


def test_the_mock_ports_match_the_provider_urls_in_the_config():
    from app.config import PROJECT_ROOT

    config = (PROJECT_ROOT / "data" / "gateway_config.sample.json").read_text(encoding="utf-8")
    assert f":{dev_stack.ALPHA_PORT}/" in config and f":{dev_stack.BETA_PORT}/" in config


def test_port_in_use_sees_a_listener_and_a_free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert dev_stack.port_in_use(listener.getsockname()[1]) is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert dev_stack.port_in_use(free) is False


def test_wait_healthy_is_true_for_a_service_that_answers_200():
    server = serve(200)
    try:
        assert dev_stack.wait_healthy(server.server_address[1], FakeProc(), timeout_s=3) is True
    finally:
        server.shutdown()


def test_wait_healthy_is_false_for_a_non_200_answer_a_dead_process_and_a_silent_port():
    server = serve(500)
    try:
        assert dev_stack.wait_healthy(server.server_address[1], FakeProc(), timeout_s=0.6) is False
        assert dev_stack.wait_healthy(server.server_address[1], FakeProc(exit_code=1), timeout_s=3) is False
    finally:
        server.shutdown()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert dev_stack.wait_healthy(free, FakeProc(), timeout_s=0.6) is False


def test_tail_returns_the_last_lines_and_survives_a_missing_file(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i}" for i in range(30)), encoding="utf-8")
    assert dev_stack.tail(log, 3) == "line 27\nline 28\nline 29"
    assert dev_stack.tail(tmp_path / "missing.log") == "(no log)"


def test_shutdown_handlers_turn_signals_into_the_cleanup_path():
    import signal

    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # what a background job inherits
        dev_stack.install_shutdown_handlers()
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # no longer ignored
        handler = signal.getsignal(signal.SIGTERM)
        with __import__("pytest").raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        for number, previous in saved.items():
            signal.signal(number, previous)
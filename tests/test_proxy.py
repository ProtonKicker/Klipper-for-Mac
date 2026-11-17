"""proxy.py: real local integration of the static + reverse-proxy server.

Fully offline: two stdlib HTTP servers on ephemeral ports inside the test.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from klipperformac import proxy


class _Upstream(BaseHTTPRequestHandler):
    """Tiny fake moonraker: static JSON for API paths."""

    def do_GET(self):
        body = json.dumps({"result": {"path": self.path}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _ProxyServer(proxy.Server):
    def __init__(self, addr, handler):
        super().__init__(addr, handler)

    def handle_error(self, request, client_address):
        pass  # silence expected tracebacks (proxy tests farm 502s on purpose)


def _serve(server_cls, handler, *args):
    server = server_cls(("127.0.0.1", 0), handler, *args)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    # Wait until the listening socket actually accepts (guards the thread race
    # where the proxy reaches the upstream before it has bound).
    import socket as _socket

    host, port = server.server_address[:2]
    for _ in range(50):
        try:
            with _socket.create_connection((host, port), timeout=0.2):
                return server, thread
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("test server never started listening")


def _url(server, path):
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _handler_for(directory):
    def handler(*args, **kwargs):
        return proxy.UIPandler(*args, directory=directory, **kwargs)

    return handler


def test_static_and_proxy_routing(tmp_path):
    static = tmp_path / "ui"
    static.mkdir()
    (static / "index.html").write_text("<html>ok</html>")

    upstream, uthread = _serve(proxy.Server, _Upstream)
    try:
        proxy.UIPandler.upstream_port = upstream.server_address[1]
        p, pthread = _serve(_ProxyServer, _handler_for(str(static)))
        try:
            # Static file served from the UI dir.
            with urllib.request.urlopen(_url(p, "/index.html"), timeout=5) as r:
                assert r.status == 200
                assert r.read() == b"<html>ok</html>"

            # API path relayed to the (fake) moonraker upstream.
            with urllib.request.urlopen(_url(p, "/printer/info"), timeout=5) as r:
                body = json.loads(r.read())
                assert body["result"]["path"] == "/printer/info"
        finally:
            pthread.join(timeout=2)
            p.server_close()
    finally:
        uthread.join(timeout=2)
        upstream.server_close()


def test_proxy_502_when_upstream_down(tmp_path):
    static = tmp_path / "ui"
    static.mkdir()
    p, thread = _serve(_ProxyServer, _handler_for(str(static)))
    try:
        # Bind a victim port, then close it so the upstream connect fails.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proxy.UIPandler.upstream_port = port
        url = f"http://127.0.0.1:{p.server_address[1]}/printer/info"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url, timeout=5)
        assert e.value.code == 502
    finally:
        thread.join(timeout=2)
        p.server_close()


def test_is_api_classification():
    h = proxy.UIPandler.__new__(proxy.UIPandler)
    for path, expected in (
        ("/websocket", True),
        ("/printer/info?x=1", True),
        ("/server/info", True),
        ("/index.html", False),
        ("/", False),
        ("/config.json", False),
    ):
        h.path = path
        assert h.is_api() is expected, path

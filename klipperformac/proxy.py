"""Combined static + reverse-proxy server for the web UIs.

Mainsail (moonraker-js) derives its REST base from the page origin and only
partially honors config.json hostname/port, so a plain static server on a
separate port breaks it (the "initializing" hang). This serves the UI's
static files AND relays Moonraker API paths + /websocket on the same port,
the same job nginx does in a standard Klipper install — stdlib only.
"""
import http.client
import select
import socket
import socketserver
import sys
from http.server import SimpleHTTPRequestHandler

API_PREFIXES = (
    "server", "printer", "access", "machine", "job_queue", "history",
    "update", "data_store", "webcam", "extensions", "api", "websocket",
    "moonraker",
)
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
              "proxy-authorization", "te", "trailer", "transfer-encoding",
              "upgrade"}


class UIPandler(SimpleHTTPRequestHandler):
    upstream_port = 7125

    def is_api(self):
        first = self.path.lstrip("/").split("/", 1)[0].split("?", 1)[0]
        return first in API_PREFIXES

    def _body(self):
        n = self.headers.get("Content-Length")
        return self.rfile.read(int(n)) if n else None

    def _proxy(self, extra_body=None):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port,
                                              timeout=15)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "connection")}
            body = extra_body if extra_body is not None else self._body()
            conn.request(self.command, self.path, body, headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as e:
            self.send_error(502, "proxy error: {}".format(e))

    def _proxy_ws(self):
        try:
            up = socket.create_connection(("127.0.0.1", self.upstream_port),
                                          timeout=15)
        except OSError:
            self.send_error(502, "websocket upstream unavailable")
            return
        head = "{} {} {}\r\n".format(self.command, self.path, self.request_version)
        for k, v in self.headers.items():
            head += "{}: {}\r\n".format(k, v)
        head += "\r\n"
        up.sendall(head.encode())
        self.close_connection = True
        client = self.connection
        up.setblocking(False)
        try:
            while True:
                r, _, _ = select.select([client, up], [], [], 300)
                if not r:
                    break
                for s in r:
                    try:
                        data = s.recv(65536)
                    except OSError:
                        data = b""
                    if not data:
                        return
                    peer = up if s is client else client
                    try:
                        peer.sendall(data)
                    except OSError:
                        return
        finally:
            try:
                up.close()
            except OSError:
                pass

    def do_GET(self):
        if self.path == "/websocket" or self.headers.get("Upgrade", "").lower() == "websocket":
            self._proxy_ws()
            return
        if self.is_api():
            self._proxy()
            return
        SimpleHTTPRequestHandler.do_GET(self)

    def do_HEAD(self):
        if self.is_api():
            self._proxy()
        else:
            SimpleHTTPRequestHandler.do_HEAD(self)

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        if self.is_api() or self.path == "/websocket":
            self._proxy()
        else:
            self.send_response(204)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; logs belong to moonraker/klippy


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv):
    port, directory, upstream = int(argv[1]), argv[2], int(argv[3])
    UIPandler.upstream_port = upstream
    handler = lambda *a, **kw: UIPandler(*a, directory=directory, **kw)
    with Server(("127.0.0.1", port), handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main(sys.argv)

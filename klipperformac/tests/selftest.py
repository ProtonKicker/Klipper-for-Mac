"""Live connection test: the exact endpoints the dashboards hit.

Run with the klipperformac venv python (has tornado):
  ~/.klipperformac/venv/bin/python -m klipperformac.tests.selftest

In native mode (no web UI selected) the proxy/static checks are skipped;
the direct Moonraker REST/WebSocket checks always run.
"""
import asyncio
import json
import sys
import urllib.request

from .. import paths

WS_URL = "ws://localhost:{}/websocket".format(paths.MOONRAKER_PORT)


def _origin():
    return paths.web_url() or "http://localhost:{}".format(paths.MOONRAKER_PORT)


def check_static(base_url):
    with urllib.request.urlopen(base_url + "/", timeout=8) as r:
        body = r.read().decode(errors="replace")
    if "<html" not in body.lower():
        raise AssertionError("web UI index is not HTML")
    return r.status, len(body)


def check_rest(port=None):
    port = port or paths.MOONRAKER_PORT
    url = "http://localhost:{}/server/info".format(port)
    with urllib.request.urlopen(url, timeout=8) as r:
        data = json.loads(r.read().decode())
    if not data.get("result", {}).get("klippy_connected"):
        raise AssertionError("moonraker not connected to klippy")
    return r.status


check_rest_via = check_rest


async def check_ws(url=None):
    from tornado.httpclient import HTTPRequest
    from tornado.websocket import websocket_connect
    req = HTTPRequest(url or WS_URL, headers={"Origin": _origin()})
    conn = await websocket_connect(req)
    try:
        for method, ident in (("printer.info", 1), ("server.info", 2)):
            await conn.write_message(json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": {}, "id": ident}))
            msg = json.loads(await asyncio.wait_for(conn.read_message(), 8))
            if "result" not in msg:
                raise AssertionError(method + " -> " + json.dumps(msg)[:200])
        return True
    finally:
        conn.close()


def main():
    results = []
    ui = paths.current_ui()
    base = paths.web_url()

    def run(desc, fn):
        try:
            fn()
            results.append((desc, True))
        except Exception as e:
            results.append((desc + ": " + str(e)[:120], False))

    if base:
        run("{} static".format(ui), lambda: check_static(base))
    run("moonraker REST klippy_connected", check_rest)
    run("websocket JSON-RPC (printer.info, server.info)",
        lambda: asyncio.new_event_loop().run_until_complete(check_ws()))
    if base:
        port = paths.ui_port(ui)
        run("{} proxy REST (browser path)".format(ui),
            lambda: check_rest_via(port))
        run("{} proxy websocket (browser path)".format(ui),
            lambda: asyncio.new_event_loop().run_until_complete(
                check_ws("ws://localhost:{}/websocket".format(port))))
    else:
        results.append(("native dashboard: proxy checks skipped", True))
    ok = all(r[1] for r in results)
    for desc, good in results:
        print("  {} {}".format("[ok]" if good else "[FAIL]", desc))
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

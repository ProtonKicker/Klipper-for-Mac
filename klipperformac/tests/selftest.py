"""Live connection test: the exact endpoints Mainsail hits.

Run with the klipperformac venv python (has tornado):
  ~/.klipperformac/venv/bin/python -m klipperformac.tests.selftest
"""
import asyncio
import json
import sys
import urllib.request

from .. import paths

WS_URL = "ws://localhost:{}/websocket".format(paths.MOONRAKER_PORT)
ORIGIN = paths.web_url()


def check_static():
    with urllib.request.urlopen(paths.web_url() + "/", timeout=8) as r:
        body = r.read().decode(errors="replace")
    if "<html" not in body.lower():
        raise AssertionError("mainsail index is not HTML")
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
    req = HTTPRequest(url or WS_URL, headers={"Origin": ORIGIN})
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
    try:
        status, n = check_static()
        results.append(("mainsail static {} ({} bytes)".format(status, n), True))
    except Exception as e:
        results.append(("mainsail static: " + str(e)[:100], False))
    try:
        status = check_rest()
        results.append(("moonraker REST {} klippy_connected".format(status), True))
    except Exception as e:
        results.append(("moonraker REST: " + str(e)[:100], False))
    try:
        asyncio.new_event_loop().run_until_complete(check_ws())
        results.append(("websocket JSON-RPC (printer.info, server.info)", True))
    except Exception as e:
        results.append(("websocket JSON-RPC: " + str(e)[:120], False))
    try:
        status = check_rest_via(paths.WEB_PORT)
        results.append(("mainsail proxy REST {} (browser path)".format(status), True))
    except Exception as e:
        results.append(("mainsail proxy REST: " + str(e)[:100], False))
    try:
        asyncio.new_event_loop().run_until_complete(
            check_ws("ws://localhost:{}/websocket".format(paths.WEB_PORT)))
        results.append(("mainsail proxy websocket (browser path)", True))
    except Exception as e:
        results.append(("mainsail proxy websocket: " + str(e)[:120], False))
    ok = all(r[1] for r in results)
    for desc, good in results:
        print("  {} {}".format("[ok]" if good else "[FAIL]", desc))
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

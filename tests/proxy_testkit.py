"""Small helpers shared by proxy tests.

These keep test-only server harness behavior fast and explicit without
changing the runtime proxy defaults.
"""

import threading


def start_server(httpd, poll_interval=0.01):
    """Run a socketserver in a daemon thread with a short shutdown poll.

    The stdlib default poll interval is 0.5s, which is fine for production but
    makes test teardown look artificially expensive. Proxy regressions create
    throwaway in-process servers, so a short poll keeps those tests honest
    about proxy work instead of measuring socketserver's idle shutdown wait.
    """

    thread = threading.Thread(target=lambda: httpd.serve_forever(
        poll_interval=poll_interval), daemon=True)
    thread.start()
    return thread

"""versions network surface: _get_json / latest_tag / remote_master_sha /
check_updates driven by an in-process fake transport.

HARD OFFLINE RULE. Two layers of trap:
  * socket.socket / socket.create_connection are replaced with raisers, so not
    even a bare connect or a DNS lookup is possible;
  * urllib.request.urlopen is replaced either with a raiser (the "dead network"
    tests) or with a scripted in-memory fake (the payload-logic tests).
The fake records every Request, every kwarg and every read() call, so these
tests assert on *how* the product calls the transport - never on the internet.

Nothing here touches ~/.klipperformac, a port, or /dev/cu.*.
"""
import io
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

HYP = dict(deadline=None, max_examples=60,
           suppress_health_check=[HealthCheck.function_scoped_fixture])

TAG_CHECKED = ("moonraker", "mainsail", "fluidd")
REF_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+-]*")


class FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen() returns."""

    def __init__(self, body):
        self.body = body
        self.read_calls = []
        self.closed = False

    def read(self, *args):
        self.read_calls.append(args)
        return self.body

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class Transport:
    """Scripted urlopen: responder(url) returns response bytes or an exception."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self.responses = []

    def __call__(self, req, *args, **kwargs):
        self.requests.append((req, args, kwargs))
        url = getattr(req, "full_url", str(req))
        result = self.responder(url)
        if isinstance(result, BaseException):
            raise result
        response = FakeResponse(result)
        self.responses.append(response)
        return response

    @property
    def urls(self):
        return [getattr(req, "full_url", str(req)) for req, _a, _k in self.requests]


def _by_url(tags_by_slug, commits=None):
    """Route each request: /tags -> canned tag list, /commits -> canned list."""
    def _responder(url):
        if "commits" in url:
            if commits is None:
                return b"[]"
            return json.dumps(commits).encode()
        for slug, tags in tags_by_slug.items():
            if slug in url:
                return json.dumps([{"name": t} for t in tags]).encode()
        return b"[]"
    return _responder


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Proof, not promise: any socket created in these tests aborts loudly."""
    seen = []

    def _boom(*args, **kwargs):
        seen.append((args, kwargs))
        raise AssertionError("BUG IN TEST: versions opened a real socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    return seen


@pytest.fixture
def dead_network(stack, monkeypatch):
    """Every transport call fails like a Mac with no uplink."""
    calls = []

    def _boom(req, *args, **kwargs):
        calls.append((req, args, kwargs))
        raise OSError("[Errno 8] nodename nor servname provided, or not known")

    monkeypatch.setattr(stack.versions.urllib.request, "urlopen", _boom)
    return calls


@pytest.fixture
def transport(stack, monkeypatch):
    holder = {"t": Transport(lambda url: b"[]")}

    def _dispatch(req, *args, **kwargs):
        return holder["t"](req, *args, **kwargs)

    monkeypatch.setattr(stack.versions.urllib.request, "urlopen", _dispatch)
    return holder["t"]


# --------------------------------------------------------------------------
# the offline guarantee itself
# --------------------------------------------------------------------------

def test_pure_local_paths_never_open_a_socket(stack, no_sockets, dead_network):
    stack.versions.load_pins()
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    stack.versions.load_pins()
    stack.versions._sort_key("v1.2.3")
    stack.versions.REPOS.copy()
    assert dead_network == [], "pins I/O must never build a request"
    assert no_sockets == []


def test_every_transport_failure_degrades_to_one_error_string_per_component(
        stack, transport, no_sockets):
    """Any exception type from the transport layer must be absorbed, not raised."""
    failures = [
        OSError("[Errno 8] nodename nor servname provided"),
        TimeoutError("The read operation timed out"),
        ValueError("unknown url type: ftp"),
        json.JSONDecodeError("Expecting value", "<html>", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        MemoryError("response body too large"),
        RuntimeError("unexpected"),
        urllib.error.URLError("tunnel connection failed"),
        urllib.error.HTTPError("https://api.github.com/repos/x/y/tags", 403,
                               "rate limit exceeded", {}, io.BytesIO(b"{}")),
    ]
    for exc in failures:
        transport.responder = (lambda e=exc: (lambda url: e))()
        updates, errors = stack.versions.check_updates()
        assert updates == []
        assert len(errors) == len(TAG_CHECKED), errors
        for err in errors:
            head, sep, detail = err.partition(" update check failed: ")
            assert sep, err
            assert head in TAG_CHECKED
            assert len(detail) <= 60
    assert no_sockets == []


def test_keyboard_interrupt_is_never_swallowed_by_the_update_check(stack, transport):
    """Ctrl-C during `klipperformac status` must reach the user, not be muted."""
    def _interrupted(url):
        raise KeyboardInterrupt()

    transport.responder = _interrupted
    with pytest.raises(KeyboardInterrupt):
        stack.versions.check_updates()


def test_dead_network_never_creates_or_modifies_the_lockfile(stack, dead_network,
                                                             no_sockets):
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    before = stack.paths.LOCKFILE.read_bytes()
    updates, errors = stack.versions.check_updates()
    assert updates == [] and len(errors) == len(TAG_CHECKED)
    assert stack.paths.LOCKFILE.read_bytes() == before
    assert no_sockets == []


# --------------------------------------------------------------------------
# how the product must call the transport
# --------------------------------------------------------------------------

def test_get_json_bounds_every_call_with_a_timeout(stack, transport):
    transport.responder = lambda url: b"[]"
    stack.versions._get_json("https://api.github.com/repos/x/y/tags?per_page=100")
    (_req, args, kwargs) = transport.requests[-1]
    assert not args, "urlopen must be called with keyword arguments only"
    timeout = kwargs.get("timeout")
    assert isinstance(timeout, (int, float)) and 0 < timeout <= 60, timeout


def test_get_json_is_a_plain_get_with_no_body_and_no_credentials(stack, transport):
    transport.responder = lambda url: b"[]"
    stack.versions._get_json("https://api.github.com/repos/x/y/tags?per_page=100")
    req, _args, _kwargs = transport.requests[-1]
    assert isinstance(req, urllib.request.Request)
    assert req.get_method() == "GET"
    assert req.data is None
    headers = {k.lower(): v for k, v in req.headers.items()}
    assert headers.get("user-agent") == "klipperformac"
    assert "application/vnd.github+json" in headers.get("accept", "")
    assert not any("authorization" in k for k in headers)
    assert not any("cookie" in k for k in headers)
    assert "@" not in req.full_url
    parts = urllib.parse.urlsplit(req.full_url)
    assert (parts.scheme, parts.netloc) == ("https", "api.github.com")
    assert not parts.username and not parts.password


def test_get_json_closes_the_response(stack, transport):
    transport.responder = lambda url: b"[]"
    stack.versions._get_json("https://api.github.com/repos/x/y/tags")
    assert transport.responses[-1].closed is True


def test_check_updates_makes_exactly_one_request_per_checked_component(stack,
                                                                      transport):
    transport.responder = _by_url({"Arksine/moonraker": ["v9.9.9"]},
                                  commits=[{"sha": "c" * 40}])
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert len(transport.requests) == len(TAG_CHECKED)
    for slug in stack.versions.REPOS.values():
        if slug == "Klipper3d/klipper":
            continue
        assert any(slug in url for url in transport.urls), slug
    assert not any("commits" in url for url in transport.urls), \
        "the klipper master check must not run for a sha pin"


def test_latest_tag_and_master_sha_hit_the_documented_endpoints(stack, transport):
    transport.responder = _by_url({"meteyou/mainsail": ["v3.0.0"]},
                                  commits=[{"sha": "d" * 40}])
    assert stack.versions.latest_tag("mainsail") == "v3.0.0"
    assert stack.versions.remote_master_sha() == "d" * 40
    assert transport.urls == [
        stack.versions.API.format("meteyou/mainsail"),
        "https://api.github.com/repos/Klipper3d/klipper/commits?per_page=1",
    ]


def test_latest_tag_rejects_an_unknown_component_without_a_request(stack, transport):
    with pytest.raises(KeyError):
        stack.versions.latest_tag("does-not-exist")
    with pytest.raises(KeyError):
        stack.versions.latest_tag("")
    assert transport.requests == [], "no request may leave for an unknown component"


# --------------------------------------------------------------------------
# response bodies: every malformed shape must degrade, never crash
# --------------------------------------------------------------------------

BODIES = [
    pytest.param(b"", id="empty-body"),
    pytest.param(b"\xff\xfe\x00garbage", id="invalid-utf8"),
    pytest.param(b"<html><body>captive portal</body></html>", id="html"),
    pytest.param(b'{"message": "Not Found"}', id="object-not-array"),
    pytest.param(b"null", id="null"),
    pytest.param(b"[]", id="empty-array"),
    pytest.param(b'[{"name": 42}]', id="numeric-name"),
    pytest.param(b'[{"commit": "abc"}]', id="missing-name"),
    pytest.param(b'["v1.2.3"]', id="array-of-strings"),
    pytest.param(b'[[1,2,3], null, {"name": "v1.0.0"}]', id="mixed-entries"),
    pytest.param(b'[{"name": "v1.0.0"},]', id="json5-trailing-comma"),
    pytest.param(b'{"klipper": "master", "klipper_sha": "' + b"a" * 40 + b'"}',
                 id="lockfile-as-response"),
    pytest.param(b"\xef\xbb\xbf" + b'[{"name": "v1.0.0"}]', id="utf8-bom"),
    pytest.param(b'[{"name": "' + b"x" * 100_000 + b'"}]', id="100k-char-tag"),
]


@pytest.mark.parametrize("body", BODIES)
def test_any_response_body_leaves_check_updates_returning_two_lists(stack, transport,
                                                                   no_sockets, body):
    transport.responder = lambda url: body
    updates, errors = stack.versions.check_updates()
    assert isinstance(updates, list) and isinstance(errors, list)
    for entry in updates:
        assert isinstance(entry, tuple) and len(entry) == 3
        assert all(isinstance(part, str) for part in entry)
    assert no_sockets == []


# --------------------------------------------------------------------------
# update-selection logic against canned tag lists (still no sockets)
# --------------------------------------------------------------------------

def test_check_updates_is_deterministic_and_touches_no_state(stack, transport):
    transport.responder = _by_url({"Arksine/moonraker": ["v99.0.0"],
                                   "meteyou/mainsail": ["v1.0.0"]},
                                  commits=[{"sha": "c" * 40}])
    shipped = dict(stack.versions.DEFAULT_PINS)
    repos = dict(stack.versions.REPOS)
    first = stack.versions.check_updates()
    second = stack.versions.check_updates()
    assert first == second
    assert stack.versions.DEFAULT_PINS == shipped
    assert stack.versions.REPOS == repos
    assert not stack.paths.LOCKFILE.exists(), "check_updates must not write pins"


def test_kipper_master_pin_without_a_recorded_sha_is_never_guessed(stack, transport):
    transport.responder = _by_url({}, commits=[{"sha": "b" * 40}])
    stack.versions.save_pins({"klipper": "master"})
    updates, errors = stack.versions.check_updates()
    assert [c for c, _a, _b in updates] == []
    assert errors == []
    assert not any("commits" in url for url in transport.urls)


def test_master_branch_update_reports_seven_char_shas(stack, transport):
    transport.responder = _by_url({}, commits=[{"sha": "b" * 40}])
    stack.versions.save_pins({"klipper": "master", "klipper_sha": "a" * 40})
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert updates == [("klipper", "a" * 7, "b" * 7 + " (master)")]


def test_main_branch_pin_is_compared_against_the_default_branch_head(stack,
                                                                    transport):
    transport.responder = _by_url({}, commits=[{"sha": "c" * 39 + "d"}])
    stack.versions.save_pins({"klipper": "main", "klipper_sha": "a" * 40})
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert len(updates) == 1
    comp, current, new = updates[0]
    assert comp == "klipper"
    assert current == "a" * 7, "the local sha is abbreviated for the CLI row"
    assert new.startswith("c" * 7), "the remote sha must be abbreviated too"


ASCII_TAG = r"v?[0-9]{1,4}(\.[0-9]{1,4}){0,3}"
ASCII_RELEASE = r"v?[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}"


@given(tags=st.lists(st.from_regex(ASCII_TAG, fullmatch=True), max_size=25))
@settings(**HYP)
def test_latest_tag_never_invents_a_tag_and_always_returns_the_maximum(stack,
                                                                      transport,
                                                                      tags):
    key = stack.versions._sort_key
    transport.responder = _by_url({"Arksine/moonraker": tags})
    result = stack.versions.latest_tag("moonraker")
    candidates = [t for t in tags if t and t[0] in "v0123456789"]
    if not candidates:
        assert result is None
        return
    assert result in candidates
    assert key(result) == max(key(t) for t in candidates)


@given(tags=st.lists(st.from_regex(ASCII_RELEASE, fullmatch=True), max_size=15))
@settings(**HYP)
def test_check_updates_only_ever_offers_a_real_upgrade(stack, transport, tags):
    key = stack.versions._sort_key
    pin = "v5.5.5"
    transport.responder = _by_url({"Arksine/moonraker": tags},
                                  commits=[{"sha": "c" * 40}])
    stack.versions.save_pins({"moonraker": pin})
    updates, errors = stack.versions.check_updates()
    assert errors == []
    offered = [u for u in updates if u[0] == "moonraker"]
    best = max((key(t) for t in tags), default=(0, 0, 0))
    if best > key(pin):
        assert len(offered) == 1
        assert offered[0][1] == pin
        assert key(offered[0][2]) == best
    else:
        assert offered == []


@given(tags=st.lists(st.from_regex(r"[a-uA-UW-Z_][A-Za-z0-9._-]{0,12}",
                                   fullmatch=True), max_size=8))
@settings(**HYP)
def test_names_that_are_not_release_looking_are_never_offered(stack, transport, tags):
    """Only 'v'/digit-initial names may reach the pin store, whatever they sort to."""
    transport.responder = _by_url({"Arksine/moonraker": tags})
    stack.versions.save_pins({"moonraker": "v0.0.1"})
    result = stack.versions.latest_tag("moonraker")
    assert result is None
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert [u for u in updates if u[0] == "moonraker"] == []


# --------------------------------------------------------------------------
# BUG DEEP
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: check_updates() compares _sort_key(pins[comp]) against the upstream "
    "tags (versions.py:103) even when the pin is a 40-char commit sha, and "
    "_sort_key() collapses any sha to (0, 0, 0). Commit pinning of "
    "moonraker/mainsail/fluidd is documented (versions.py:14-17) and "
    "`klipperformac update --pin moonraker=<sha>` is accepted by cli.py:279-282, so "
    "every such install shows a permanent fake 'update available' in `status` and "
    "the TUI, and `update --apply` silently moves the pin off the audited commit "
    "onto a tag"))
def test_a_commit_pin_is_never_reported_as_outdated(stack, transport):
    sha = stack.versions.DEFAULT_PINS["klipper"]
    transport.responder = _by_url({"Arksine/moonraker": ["v0.99.0"]})
    stack.versions.save_pins({"moonraker": sha})
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert updates == [], "sha pin {} was compared against tags".format(sha)


@pytest.mark.parametrize("tag", [
    pytest.param("v9.9.9 beta", id="space"),
    pytest.param("v9.9.9\n", id="trailing-newline"),
    pytest.param("v9.9.9\tshipped", id="tab"),
    pytest.param("v9.9.9../../evil", id="dot-dot-slashes"),
    pytest.param("v9.9.9\U0001f600", id="emoji"),
    pytest.param("v9.9.9;open -a Calculator", id="shell-looking"),
    pytest.param("v", id="bare-v"),
    pytest.param("v-v-v", id="no-digits-at-all"),
])
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: latest_tag() filters upstream tags on the FIRST character only "
    "(versions.py:71-74) and check_updates() hands the raw upstream string to its "
    "caller (versions.py:104), where cli.cmd_update --apply stores it in the pin "
    "file. The filter therefore accepts the single-character names 'v' and 'V' as "
    "'the most version-like tag upstream', and because _sort_key() ignores "
    "everything after the digits, git tag names containing spaces, tabs and "
    "non-ASCII score as real releases - 'v9.9.9 beta' becomes (9, 9, 0) and is "
    "offered as an upgrade. That unusable value is then passed to "
    "installer.checkout() -> `git clone --branch <ref>`, spliced by fetch_webui() "
    "into a releases/download URL, and printed unsanitised by `status`. Candidates "
    "must be validated (real dotted version, ref-safe, URL-path-safe) before being "
    "returned or offered"))
def test_only_usable_release_names_can_be_offered_as_an_update(stack, transport, tag):
    transport.responder = _by_url({"Arksine/moonraker": [tag]})
    stack.versions.save_pins({"moonraker": "v0.1.0"})
    result = stack.versions.latest_tag("moonraker")
    if result is not None:
        assert re.fullmatch(REF_SAFE.pattern, result), repr(result)
        assert any(ch in "0123456789" for ch in result), \
            "{} is not a version".format(repr(result))
    updates, errors = stack.versions.check_updates()
    assert errors == []
    for comp, _cur, new in updates:
        assert re.fullmatch(REF_SAFE.pattern, new), \
            "{} offered an unusable tag {!r}".format(comp, new)
        assert new.isprintable() and " " not in new


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: the remote payload is trusted exactly like the local one - "
    "remote_master_sha() returns data[0]['sha'] verbatim and check_updates() "
    "slices it OUTSIDE its try/except (versions.py:95-96), so a response whose sha "
    "field is a number or null raises TypeError ('int' object is not subscriptable) "
    "straight out of `klipperformac status` / `update` / the TUI instead of being "
    "reported as an update-check failure. _get_json() validates no schema at all"))
def test_a_non_string_remote_sha_degrades_to_an_error_string(stack, transport):
    transport.responder = _by_url({}, commits=[{"sha": 123456}])
    stack.versions.save_pins({"klipper": "master", "klipper_sha": "a" * 40})
    updates, errors = stack.versions.check_updates()
    assert isinstance(updates, list)
    assert all(isinstance(e, str) for e in errors)


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: the update-check error strings are truncated (str(e)[:60] at "
    "versions.py:93-102) but never sanitised, and str(HTTPError) embeds the "
    "*server controlled* reason phrase. A proxy or captive portal that answers the "
    "update check with an ANSI / CR / LF carrying reason therefore injects raw "
    "escape sequences and fake extra lines into `klipperformac status` (cli.py:156 "
    "prints '  [!] ' + e) and into the TUI message row. Control characters must be "
    "stripped before a transport error is displayed"))
def test_update_check_errors_never_carry_control_characters(stack, transport):
    hostile = "\x1b[2J\x1b[H pwned\r\n[!] fake line " + "x" * 200
    exc = urllib.error.HTTPError("https://api.github.com/repos/x/y/tags", 500,
                                 hostile, {}, io.BytesIO(b"{}"))
    transport.responder = lambda url: exc
    _updates, errors = stack.versions.check_updates()
    assert errors
    for err in errors:
        assert "\x1b" not in err and "\r" not in err and "\n" not in err, repr(err)
        assert all(ord(ch) >= 0x20 or ch == " " for ch in err), repr(err)


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _get_json() slurps the whole response with an unbounded r.read() "
    "(versions.py:51-52) while installer._download() enforces max_bytes=200MB for "
    "exactly this reason (installer.py:181-192). A hostile or MITM'd endpoint can "
    "answer the update check that `klipperformac status` runs on every invocation "
    "with an unbounded body and exhaust memory. The body read must be size bounded "
    "(chunked read + cap) before json.loads() sees it"))
def test_get_json_reads_the_body_through_a_size_bounded_read(stack, transport):
    transport.responder = lambda url: b"[]"
    stack.versions._get_json("https://api.github.com/repos/x/y/tags")
    read_calls = transport.responses[-1].read_calls
    assert read_calls, "the body was never read"
    assert any(call and isinstance(call[0], int) and call[0] <= 4_000_000
               for call in read_calls), \
        "read() must ask for a bounded number of bytes, got {}".format(read_calls)

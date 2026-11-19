"""versions REPOS/lockfile shape + every network-touching entry point.

HARD OFFLINE RULE: urllib.request.urlopen is monkeypatched to raise in every
test here, so nothing can reach the network. Where update-check *logic* needs a
response, the in-process seam versions._get_json is stubbed with canned data and
the test asserts urlopen was still never called.
"""
import json
import re
import urllib.parse

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

LONG_MSG = "network is unreachable for the offline test harness: " + "x" * 400

EXPECTED_REPOS = {
    "klipper": "Klipper3d/klipper",
    "moonraker": "Arksine/moonraker",
    "mainsail": "meteyou/mainsail",
    "fluidd": "fluidd-core/fluidd",
}

# components check_updates() compares against upstream tags
TAG_CHECKED = ("moonraker", "mainsail", "fluidd")


@pytest.fixture
def spy_urlopen(stack, monkeypatch):
    """urlopen always raises; returns the list of (args, kwargs) it saw."""
    seen = []

    def _boom(*args, **kwargs):
        seen.append((args, kwargs))
        raise OSError(LONG_MSG)

    monkeypatch.setattr(stack.versions.urllib.request, "urlopen", _boom)
    return seen


@pytest.fixture
def canned(stack, monkeypatch, spy_urlopen):
    """Stub the in-process JSON seam; payload is whatever payload() returns."""
    holder = {"payload": None, "urls": []}

    def _fake_get_json(url):
        holder["urls"].append(url)
        if callable(holder["payload"]):
            return holder["payload"](url)
        return holder["payload"]

    monkeypatch.setattr(stack.versions, "_get_json", _fake_get_json)
    return holder


def _tags(*names):
    return [{"name": n} for n in names]


# --------------------------------------------------------------------------
# REPOS / DEFAULT_PINS / API shape sanity (offline, pure data)
# --------------------------------------------------------------------------

def test_repos_structure(stack):
    versions = stack.versions
    assert set(versions.REPOS) == set(EXPECTED_REPOS)
    assert versions.REPOS == EXPECTED_REPOS, "upstream identities must not drift"


def test_repos_values_are_owner_slash_repo(stack):
    for comp, repo in stack.versions.REPOS.items():
        assert isinstance(repo, str) and repo, comp
        assert "://" not in repo and not repo.startswith("/")
        owner, _, name = repo.partition("/")
        assert owner and name and "/" not in name
        assert repo == repo.strip()
        assert not repo.endswith(".git")


def test_api_template_produces_a_https_github_url_for_every_repo(stack):
    versions = stack.versions
    assert versions.API.count("{}") == 1
    assert "per_page=100" in versions.API
    for comp, repo in versions.REPOS.items():
        url = versions.API.format(repo)
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https", comp
        assert parts.netloc == "api.github.com", comp
        assert parts.path == "/repos/{}/tags".format(repo), comp
        assert not parts.username and not parts.password, "no credentials in URL"


@given(repo=st.from_regex(r"[A-Za-z0-9._-]{1,40}/[A-Za-z0-9._-]{1,40}",
                          fullmatch=True))
@settings(deadline=None, max_examples=60,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_api_url_never_escapes_the_github_host(stack, repo):
    url = stack.versions.API.format(repo)
    parts = urllib.parse.urlsplit(url)
    assert parts.netloc == "api.github.com"
    assert parts.path.startswith("/repos/")
    assert "\n" not in url and " " not in url


def test_default_pins_cover_every_repo_and_the_ui(stack):
    versions = stack.versions
    assert set(versions.REPOS) <= set(versions.DEFAULT_PINS)
    assert set(versions.DEFAULT_PINS) == set(versions.REPOS) | {"ui"}
    assert versions.DEFAULT_PINS["ui"] in ("mainsail", "fluidd")
    assert versions.DEFAULT_PINS["ui"] in versions.REPOS
    for key, value in versions.DEFAULT_PINS.items():
        assert isinstance(value, str) and value, key


def test_tag_pins_are_sortable_by_sort_key(stack):
    """Pins compared with _sort_key() must parse as real 3-part versions."""
    versions = stack.versions
    for comp in TAG_CHECKED:
        pin = versions.DEFAULT_PINS[comp]
        assert re.fullmatch(r"v\d+\.\d+\.\d+", pin), comp
        key = versions._sort_key(pin)
        assert len(key) == 3
        assert key == tuple(int(p) for p in pin.lstrip("v").split("."))
        assert key != (0, 0, 0), pin


def test_kipper_pin_is_a_full_commit_or_a_tracked_branch(stack):
    versions = stack.versions
    pin = versions.DEFAULT_PINS["klipper"]
    if pin in ("master", "main"):
        pytest.skip("branch-tracked default pins")
    assert re.fullmatch(r"[0-9a-f]{40}", pin)


def test_lockfile_pins_have_the_shape_lockfile_saves(stack):
    """save_pins/load_pins round-trip must keep every key check_updates indexes."""
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    raw = json.loads(stack.paths.LOCKFILE.read_text())
    assert isinstance(raw, dict)
    assert set(raw) == set(stack.versions.DEFAULT_PINS)
    pins = stack.versions.load_pins()
    for comp in TAG_CHECKED:
        assert comp in pins
        stack.versions._sort_key(pins[comp])          # must not raise
    assert "klipper" in pins and "klipper_sha" not in pins


def test_check_updates_monitored_set_matches_repos(stack, spy_urlopen):
    """Behaviourally: exactly the REPOS minus klipper are tag-compared."""
    _updates, errors = stack.versions.check_updates()
    comps = {e.split(" update check failed")[0] for e in errors}
    assert comps == set(EXPECTED_REPOS) - {"klipper"} == set(TAG_CHECKED)
    assert comps <= set(stack.versions.REPOS)


# --------------------------------------------------------------------------
# offline guarantees
# --------------------------------------------------------------------------

def test_pins_io_never_touches_the_network(stack, spy_urlopen):
    stack.versions.load_pins()
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    stack.versions.load_pins()
    stack.versions._sort_key("v1.2.3")
    assert spy_urlopen == [], "pins I/O must be purely local"


def test_module_import_and_data_access_touch_no_network(stack, spy_urlopen):
    for value in (stack.versions.REPOS, stack.versions.DEFAULT_PINS,
                  stack.versions.API):
        assert value is not None
    assert spy_urlopen == []


def test_urlopen_dead_network_check_updates_degrades(stack, spy_urlopen):
    updates, errors = stack.versions.check_updates()
    assert updates == []
    assert len(errors) == len(TAG_CHECKED)
    for err in errors:
        assert " update check failed: " in err
        head, _, detail = err.partition(" update check failed: ")
        assert head in TAG_CHECKED
        assert len(detail) <= 60, "error text must stay truncated for the CLI"
    assert len(spy_urlopen) == len(TAG_CHECKED)


def test_check_updates_does_not_write_when_network_is_dead(stack, spy_urlopen):
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    before = stack.paths.LOCKFILE.read_bytes()
    stack.versions.check_updates()
    assert stack.paths.LOCKFILE.read_bytes() == before


def test_check_updates_master_pin_with_dead_network(stack, spy_urlopen):
    stack.versions.save_pins({"klipper": "master", "klipper_sha": "a" * 40})
    updates, errors = stack.versions.check_updates()
    assert updates == []
    assert any(e.startswith("klipper update check failed: ") for e in errors)
    klipper_err = [e for e in errors if e.startswith("klipper")][0]
    detail = klipper_err.partition(": ")[2]
    assert len(detail) <= 80


def test_get_json_propagates_transport_errors(stack, spy_urlopen):
    with pytest.raises(OSError):
        stack.versions._get_json("https://api.github.com/repos/x/y/tags")
    assert len(spy_urlopen) == 1


def test_latest_tag_and_master_sha_propagate_errors(stack, spy_urlopen):
    with pytest.raises(OSError):
        stack.versions.latest_tag("moonraker")
    with pytest.raises(OSError):
        stack.versions.remote_master_sha()
    assert len(spy_urlopen) == 2


def test_latest_tag_requests_the_right_url_and_headers(stack, spy_urlopen):
    for comp, repo in EXPECTED_REPOS.items():
        with pytest.raises(OSError):
            stack.versions.latest_tag(comp)
    reqs = [args[0] for args, _ in spy_urlopen]
    urls = [r.full_url for r in reqs]
    assert urls == [stack.versions.API.format(r) for r in EXPECTED_REPOS.values()]
    for req in reqs:
        assert req.get_method() == "GET"
        headers = {k.lower(): v for k, v in req.headers.items()}
        assert headers.get("user-agent") == "klipperformac"
        assert "application/vnd.github+json" in headers.get("accept", "")


def test_remote_master_sha_requests_kipper_commits(stack, spy_urlopen):
    with pytest.raises(OSError):
        stack.versions.remote_master_sha()
    (args, _kwargs), = spy_urlopen
    assert args[0].full_url == (
        "https://api.github.com/repos/Klipper3d/klipper/commits?per_page=1")


# --------------------------------------------------------------------------
# update-check logic, driven by canned payloads (urlopen still never used)
# --------------------------------------------------------------------------

def test_newer_remote_tag_is_reported(stack, canned, spy_urlopen):
    canned["payload"] = _tags("v0.99.0", "v0.10.0")
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert ("moonraker", stack.versions.DEFAULT_PINS["moonraker"], "v0.99.0") \
        in updates
    assert spy_urlopen == [], "canned test must not have hit urllib"


def test_equal_or_older_remote_tag_is_not_reported(stack, canned):
    pins = dict(stack.versions.DEFAULT_PINS)
    canned["payload"] = lambda url: _tags(pins["moonraker"], "v0.1.0") \
        if "Arksine" in url else []
    updates, _errors = stack.versions.check_updates()
    assert [c for c, _a, _b in updates] == []


def test_max_tag_across_a_huge_response(stack, canned):
    canned["payload"] = lambda url: (
        _tags(*["v0.{}".format(i) for i in range(20_000)]) if "Arksine" in url
        else [])
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert ("moonraker", stack.versions.DEFAULT_PINS["moonraker"], "v0.19999") \
        in updates


def test_master_branch_update_is_reported_with_short_shas(stack, canned):
    canned["payload"] = lambda url: (
        [{"sha": "b" * 40}] if url.endswith("commits?per_page=1") else [])
    stack.versions.save_pins({"klipper": "master", "klipper_sha": "a" * 40})
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert ("klipper", "aaaaaaa", "bbbbbbb (master)") in updates


def test_master_branch_unchanged_sha_reports_nothing(stack, canned):
    canned["payload"] = lambda url: (
        [{"sha": "a" * 40}] if url.endswith("commits?per_page=1") else [])
    stack.versions.save_pins({"klipper": "master", "klipper_sha": "a" * 40})
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert [c for c, _a, _b in updates] == []


def test_empty_tag_list_is_not_an_error(stack, canned):
    canned["payload"] = []
    assert stack.versions.latest_tag("moonraker") is None
    updates, errors = stack.versions.check_updates()
    assert (updates, errors) == ([], [])


@pytest.mark.parametrize("payload", [
    pytest.param(_tags(""), id="empty-tag-name"),
    pytest.param([{"commit": "sha"}], id="missing-name-key"),
    pytest.param(["v1.2.3"], id="list-of-strings"),
    pytest.param({"result": _tags("v1.0.0")}, id="payload-is-dict"),
    pytest.param(None, id="payload-is-null"),
    pytest.param(_tags("v1.0.0") + [None], id="null-entry"),
    pytest.param(_tags("v1.0.0") + [[1, 2]], id="list-entry"),
    pytest.param(_tags("v1.0.0") + [{"name": 123}], id="numeric-name"),
])
def test_malformed_api_payload_degrades_to_an_error_string(stack, canned, payload):
    """latest_tag() is allowed to raise, but check_updates() must absorb it."""
    canned["payload"] = payload
    updates, errors = stack.versions.check_updates()
    assert updates == []
    assert len(errors) == len(TAG_CHECKED)
    for err in errors:
        assert " update check failed: " in err


def test_unordered_tag_list_still_picks_the_true_maximum(stack, canned):
    names = ["v1.2.10", "v1.2.9", "v1.10.0", "v1.9.0", "v10.0.0", "v0.999.999"]
    canned["payload"] = lambda url: _tags(*reversed(names)) \
        if "Arksine" in url else []
    updates, _errors = stack.versions.check_updates()
    assert ("moonraker", stack.versions.DEFAULT_PINS["moonraker"], "v10.0.0") \
        in updates


def test_tags_not_starting_with_v_or_a_digit_are_ignored(stack, canned):
    canned["payload"] = lambda url: (
        _tags("nightly-99.99.99", "release-99", "zzz-v99", "v0.9.9")
        if "Arksine" in url else [])
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert updates == [], "non-release tags must never be offered as an update"


# --------------------------------------------------------------------------
# BUG DEEP
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    '{"moonraker": null}',
    '{"mainsail": 2.19}',
    '{"fluidd": [1, 37, 6]}',
    '{"moonraker": {"tag": "v0.11.0"}}',
    '{"klipper": "master", "klipper_sha": 123456}',
])
def test_check_updates_tolerates_wrong_typed_pins(stack, canned, payload):
    canned["payload"] = lambda url: (
        [{"sha": "b" * 40}] if url.endswith("commits?per_page=1")
        else _tags("v0.99.0"))
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.LOCKFILE.write_text(payload)
    updates, errors = stack.versions.check_updates()
    assert isinstance(updates, list)
    assert isinstance(errors, list)


def test_latest_tag_prefers_stable_over_prerelease_on_a_tie(stack, canned):
    canned["payload"] = _tags("v2.0.0-rc1", "v2.0.0")
    assert stack.versions.latest_tag("mainsail") == "v2.0.0"


def test_latest_tag_skips_bad_entries_instead_of_failing(stack, canned):
    canned["payload"] = _tags("v9.9.9", "") + [{"commit": "no-name"}]
    assert stack.versions.latest_tag("moonraker") == "v9.9.9"

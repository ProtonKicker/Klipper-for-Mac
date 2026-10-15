"""versions REPOS / DEFAULT_PINS / API: structural sanity vs the lockfile shape.

The 'stack' fixture gives a throwaway KLIPPERFORMAC_HOME, so every test here can
also prove the cross-module contract: the pin set the CLI, TUI and installer
index by name (klipper, moonraker, mainsail, fluidd, ui, <comp>_sha) must always
come back from load_pins(), whatever a user typed into lockfile.json.

Offline by construction: urllib.request.urlopen and socket.socket are trapped.
"""
import importlib
import json
import re
import socket
import urllib.parse

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

HYP = dict(deadline=None, max_examples=80,
           suppress_health_check=[HealthCheck.function_scoped_fixture])

# keys the rest of the product indexes on versions.load_pins()
CLI_INDEXED = ("klipper", "moonraker", "mainsail", "fluidd", "ui")
GIT_COMPONENTS = ("klipper", "moonraker")          # installer.checkout(ref)
WEB_COMPONENTS = ("mainsail", "fluidd")            # releases/download/<tag>/<c>.zip
TAG_CHECKED = ("moonraker", "mainsail", "fluidd")  # check_updates compares tags

REF_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+-]*")
SHA40 = re.compile(r"[0-9a-f]{40}")
RELEASE_TAG = re.compile(r"v?\d+(\.\d+)*([-+.~][A-Za-z0-9._-]+)?")


@pytest.fixture(autouse=True)
def offline(stack, monkeypatch):
    seen = []

    def _no_socket(*args, **kwargs):
        seen.append(("socket", args))
        raise AssertionError("BUG IN TEST: REPOS sanity opened a socket")

    def _no_urlopen(*args, **kwargs):
        seen.append(("urlopen", args))
        raise AssertionError("BUG IN TEST: REPOS sanity hit the network")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)
    monkeypatch.setattr(stack.versions.urllib.request, "urlopen", _no_urlopen)
    return seen


# --------------------------------------------------------------------------
# REPOS / API / DEFAULT_PINS are internally consistent data
# --------------------------------------------------------------------------

def test_repos_is_a_str_dict_with_github_owner_repo_values(stack):
    repos = stack.versions.REPOS
    assert isinstance(repos, dict) and repos
    assert all(isinstance(k, str) and k for k in repos)
    assert all(k == k.strip().lower() for k in repos)
    for comp, slug in repos.items():
        assert isinstance(slug, str), comp
        assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug), (comp, slug)
        assert not slug.endswith("/"), comp
        assert "//" not in slug and "\\" not in slug, comp
        assert ".." not in slug, comp
        assert slug == slug.strip(), comp
    assert len(set(repos.values())) == len(repos), "two components, one repo?"


def test_repos_and_default_pins_describe_the_same_components(stack):
    assert set(stack.versions.REPOS) <= set(stack.versions.DEFAULT_PINS)
    ui = stack.versions.DEFAULT_PINS["ui"]
    assert ui in stack.versions.REPOS, "the default UI must be a known component"
    assert set(stack.versions.DEFAULT_PINS) == set(stack.versions.REPOS) | {"ui"}


def test_every_shipped_pin_is_a_usable_git_ref_and_url_segment(stack):
    """installer interpolates pins into `git clone --branch <ref>` and into a
    https://github.com/<repo>/releases/download/<tag>/<component>.zip URL."""
    for comp, ref in stack.versions.DEFAULT_PINS.items():
        assert isinstance(ref, str) and ref, comp
        assert ref == ref.strip(), "{}: pin has stray whitespace".format(comp)
        assert not ref.startswith("-"), "{}: pin looks like a git option".format(comp)
        assert ".." not in ref and "/" not in ref, "{}: ref escapes the URL".format(comp)
        assert ref.isascii() and ref.isprintable(), comp
        urllib.parse.quote(ref, safe="")
        if comp == "ui":
            assert ref in stack.versions.REPOS, "default UI must be a component"
        elif comp in GIT_COMPONENTS:
            assert SHA40.fullmatch(ref) or REF_SAFE.fullmatch(ref), comp
        else:
            assert RELEASE_TAG.fullmatch(ref), "{}: {}".format(comp, ref)


def test_tag_checked_pins_are_the_only_ones_sort_key_can_compare(stack):
    """check_updates() compares _sort_key(pin) upstream tags, so those pins must
    be real dotted versions; a sha there would score (0, 0, 0) forever."""
    versions = stack.versions
    pins = versions.load_pins()
    for comp in TAG_CHECKED:
        key = versions._sort_key(pins[comp])
        assert key != (0, 0, 0), comp
        assert len(key) >= 3, comp
    klipper_key = versions._sort_key(pins["klipper"])
    assert klipper_key == (0, 0, 0), "the shipped klipper pin is a commit sha"


def test_api_template_is_the_only_url_for_tags_and_needs_one_argument(stack):
    api = stack.versions.API
    assert api.count("{}") == 1 and "{" not in api.replace("{}", "")
    for comp, slug in stack.versions.REPOS.items():
        url = api.format(slug)
        parts = urllib.parse.urlsplit(url)
        assert (parts.scheme, parts.netloc) == ("https", "api.github.com"), comp
        assert parts.path == "/repos/{}/tags".format(slug), comp
        assert parts.query == "per_page=100", comp
        assert ";" not in parts.path, "no path parameters allowed: " + comp
        assert not parts.username and not parts.password


def test_module_level_data_is_immutable_for_callers(stack):
    """Nothing in the public API may hand back REPOS/DEFAULT_PINS themselves."""
    shipped = dict(stack.versions.DEFAULT_PINS)
    repos = dict(stack.versions.REPOS)
    returned = stack.versions.load_pins()
    assert returned is not stack.versions.DEFAULT_PINS
    returned["klipper"] = "clobbered"
    returned.clear()
    assert stack.versions.DEFAULT_PINS == shipped
    assert stack.versions.REPOS == repos


def test_import_of_versions_never_needs_the_network_or_the_real_home(stack, offline):
    module = importlib.reload(importlib.import_module("klipperformac.versions"))
    assert module.REPOS
    assert module.load_pins() == module.DEFAULT_PINS
    assert offline == []
    assert str(module.paths.LOCKFILE).startswith(str(stack.home)), \
        "reloaded module must stay inside the throwaway KLIPPERFORMAC_HOME"


# --------------------------------------------------------------------------
# lockfile shape: what the product indexes must always come back
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    pytest.param("{}", id="empty-object"),
    pytest.param('{"ui": "mainsail"}', id="ui-only"),
    pytest.param('{"unknown": "value"}', id="unknown-key-only"),
    pytest.param('{"klipper_sha": "' + "a" * 40 + '"}', id="sha-only"),
    pytest.param('{"klipper": null, "moonraker": null}', id="nulls"),
    pytest.param('{"klipper": [], "moonraker": {}}', id="empty-containers"),
    pytest.param(json.dumps({str(i): "v" * 500 for i in range(5000)}),
                 id="five-thousand-noise-keys"),
    pytest.param(json.dumps({"nested": {"deep": {"deeper": [1, 2, {"x": None}]}}}),
                 id="deeply-nested-unknown"),
])
def test_load_pins_always_exposes_every_indexed_key(stack, payload):
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.LOCKFILE.write_text(payload)
    pins = stack.versions.load_pins()
    assert isinstance(pins, dict)
    for key in CLI_INDEXED:
        assert key in pins, "{} vanished from the pin set".format(key)
    assert set(pins) >= set(CLI_INDEXED)


def test_lockfile_can_never_delete_a_default_pin(stack):
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    for key in list(stack.versions.DEFAULT_PINS):
        stack.paths.LOCKFILE.write_text(json.dumps({k: "x" for k in
                                                    stack.versions.DEFAULT_PINS
                                                    if k != key}))
        assert key in stack.versions.load_pins()


def test_pins_written_by_the_product_are_read_back_by_the_product(stack):
    """installer.setup() and _reprovision() store <component>_sha keys; the shape
    must survive save/load so `status` and `update` keep working after setup."""
    installed = dict(stack.versions.DEFAULT_PINS)
    installed["klipper_sha"] = "a" * 40
    installed["moonraker_sha"] = "b" * 40
    stack.versions.save_pins(installed)
    back = stack.versions.load_pins()
    assert back == installed
    assert back["klipper_sha"] == "a" * 40
    assert stack.versions._sort_key(back["klipper_sha"]) == (0, 0, 0)


@given(extra=st.dictionaries(st.text(min_size=1, max_size=12),
                             st.text(max_size=12), max_size=6))
@settings(**HYP)
def test_any_extra_noise_keys_survive_without_shadowing_defaults(stack, extra):
    reserved = set(CLI_INDEXED)
    payload = {k: v for k, v in extra.items() if k not in reserved}
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.LOCKFILE.write_text(json.dumps(payload))
    pins = stack.versions.load_pins()
    for key, value in payload.items():
        assert pins[key] == value
    for key in reserved:
        assert pins[key] == stack.versions.DEFAULT_PINS[key]


# --------------------------------------------------------------------------
# cross-module drift guards: REPOS must agree with the installer tables
# --------------------------------------------------------------------------

def test_installer_clone_urls_agree_with_repos(stack):
    """check_updates() compares tags on REPOS[comp]; installer.checkout() clones
    installer.URLS[comp]. If those two tables ever disagree, `update --apply`
    silently moves a pin that was validated against a *different* upstream."""
    versions, installer = stack.versions, stack.installer
    for comp, slug in versions.REPOS.items():
        if comp in installer.URLS:
            assert installer.URLS[comp] == "https://github.com/" + slug, comp
            assert installer.URLS[comp].isascii(), comp


def test_only_git_components_have_checkout_destinations(stack):
    """cli._reprovision()/installer.setup() only call checkout() for the two git
    components; the web UIs go through releases/download URLs instead."""
    versions, installer = stack.versions, stack.installer
    assert set(installer.URLS) == set(installer.DESTS)
    assert set(installer.DESTS) <= set(versions.REPOS)
    assert set(versions.REPOS) - set(installer.DESTS) == set(WEB_COMPONENTS)
    for comp in WEB_COMPONENTS:
        tag = versions.load_pins()[comp]
        url = "https://github.com/{}/releases/download/{}/{}.zip".format(
            versions.REPOS[comp], tag, comp)
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "github.com" and parts.scheme == "https"
        assert parts.path == "/{}/releases/download/{}/{}.zip".format(
            versions.REPOS[comp], tag, comp)


def test_ui_default_matches_the_components_the_web_root_provides(stack):
    versions = stack.versions
    assert versions.DEFAULT_PINS["ui"] in WEB_COMPONENTS
    for comp in WEB_COMPONENTS:
        assert comp in versions.REPOS and comp in versions.DEFAULT_PINS
    assert stack.paths.ui_url("mainsail") != stack.paths.ui_url("fluidd")
    assert stack.paths.ui_url(versions.DEFAULT_PINS["ui"]) == \
        stack.paths.web_url()

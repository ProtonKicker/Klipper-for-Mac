"""versions.py: pins I/O + update checks with an in-process _get_json seam.

NO test here touches the network: versions._get_json is the only urlopen
call site and is monkeypatched in every network-facing test.
"""

import json


def test_load_pins_defaults(stack):
    assert stack.versions.load_pins()["ui"] == "fluidd"
    assert stack.versions.load_pins()["klipper"]


def test_load_pins_merge_lockfile(stack):
    stack.versions.save_pins({"moonraker": "v0.9.0"})
    pins = stack.versions.load_pins()
    assert pins["moonraker"] == "v0.9.0"  # lockfile wins
    assert pins["mainsail"] == "v2.19.0"  # defaults fill gaps


def test_save_pins_creates_lockfile(stack):
    stack.versions.save_pins({"ui": "mainsail"})
    assert stack.paths.LOCKFILE.exists()
    assert json.loads(stack.paths.LOCKFILE.read_text())["ui"] == "mainsail"


def _stub_latest(stack, monkeypatch, tags_by_comp, commits=None):
    """versions._get_json returns per-component tag lists (and commit SHAs
    for the klipper-master path), keyed by the repo slug in the URL."""
    repos = stack.versions.REPOS

    def fake_get_json(url):
        if "/commits" in url:
            return [{"sha": s} for s in (commits or ["c" * 40])]
        for comp, tags in tags_by_comp.items():
            if repos[comp] in url:
                return [{"name": t} for t in tags]
        raise AssertionError("unexpected URL: " + url)

    monkeypatch.setattr(stack.versions, "_get_json", fake_get_json)


def test_latest_tag_picks_max(stack, monkeypatch):
    _stub_latest(stack, monkeypatch, {"moonraker": ["v1.0.0", "v1.5.0", "dont-used", "1.5.1"]})
    assert stack.versions.latest_tag("moonraker") == "1.5.1"


def test_check_updates_reports_newer(stack, monkeypatch):
    stack.versions.save_pins({"moonraker": "v0.11.0", "mainsail": "v2.19.0", "fluidd": "v1.37.6"})
    _stub_latest(
        stack,
        monkeypatch,
        {
            "moonraker": ["v0.12.0"],
            "mainsail": ["v3.0.0"],
            "fluidd": ["v1.40.0"],
        },
    )
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert {c for c, _, _ in updates} == {"moonraker", "mainsail", "fluidd"}


def test_check_updates_none_available(stack, monkeypatch):
    stack.versions.save_pins({"moonraker": "v0.11.0", "mainsail": "v2.19.0", "fluidd": "v1.37.6"})
    _stub_latest(
        stack,
        monkeypatch,
        {
            "moonraker": ["v0.11.0"],
            "mainsail": ["v2.19.0"],
            "fluidd": ["v1.37.6"],
        },
    )
    updates, errors = stack.versions.check_updates()
    assert updates == [] and errors == []


def test_check_updates_error_degrades(stack, monkeypatch):
    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(stack.versions, "_get_json", boom)
    updates, errors = stack.versions.check_updates()
    assert updates == []
    assert len(errors) == 3  # klipper won't check unless pinned to master


def test_check_updates_master_tracks_sha(stack, monkeypatch):
    pins = stack.versions.load_pins()
    pins["klipper"] = "master"
    pins["klipper_sha"] = "a" * 40
    stack.versions.save_pins(pins)
    _stub_latest(
        stack,
        monkeypatch,
        {"moonraker": ["v0.11.0"], "mainsail": ["v2.19.0"], "fluidd": ["v1.37.6"]},
        commits=["b" * 40],
    )
    updates, errors = stack.versions.check_updates()
    assert errors == []
    assert any(c == "klipper" for c, _, _ in updates)

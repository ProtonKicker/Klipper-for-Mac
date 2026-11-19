"""Deep tests: lockfile.json as `installer` consumes it, and the setup flow.

The lockfile is the installer's only untrusted input: `setup()` reads pins from
it (versions.load_pins), writes them back, and then feeds `pins[comp]` straight
into `git clone --branch` / `git fetch` and the release-asset URL. This file
drives that flow with every external collaborator faked -- `installer._run`,
`_download`, `ensure_runtime_shims`, `configgen.ensure_configs`,
`shaper.ensure_shaper` -- so setup() is exercised as pure logic: no git, no pip,
no venv, no download, no live stack, no ~/.klipperformac.

Every test here runs against the current product code (no `xfail` marks left):
the pin-validation and pristine-gating bugs found in this area are fixed in
klipperformac/installer.py, and the corrupt/non-object lockfile recovery is
fixed in klipperformac/versions.py. One relabelled case
(test_setup_git_failures_do_not_write_a_lying_ref_marker) documents why its
expected exception changed when a failed fetch became a CLI user error.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SHA = "461c4e3722c3a897fba1c6b3f0780a5315043842"


class RunStub:
    """A `_run` replacement: answers probes, records argv, never executes."""

    def __init__(self, fail_on=(), rc_by_pattern=None, sha=SHA):
        self.calls = []
        self.fail_on = list(fail_on)
        self.rc_by_pattern = dict(rc_by_pattern or {})
        self.sha = sha

    def __call__(self, cmd, **kw):
        argv = [str(c) for c in cmd]
        joined = " ".join(argv)
        self.calls.append((argv, kw))
        for needle in self.fail_on:
            if needle in joined:
                return self._fail(argv, kw, 1)
        for needle, code in self.rc_by_pattern.items():
            if needle in joined:
                return self._fail(argv, kw, code)
        out = (self.sha + "\n").encode() if "rev-parse" in joined else b"3.12\n"
        return subprocess.CompletedProcess(argv, 0, out, b"")

    @staticmethod
    def _fail(argv, kw, code):
        if kw.get("check"):
            raise subprocess.CalledProcessError(code, argv, b"", b"boom")
        return subprocess.CompletedProcess(argv, code, b"", b"boom")

    def joined_all(self):
        return [" ".join(a) for a, _ in self.calls]

    def find(self, needle):
        return [a for a, _ in self.calls if needle in " ".join(a)]


class SetupHarness:
    """Every side effect of setup() recorded, none of them performed."""

    def __init__(self, stack, monkeypatch, real_checkout=False):
        self.stack = stack
        self.order = []
        self.checkouts = []
        self.fetches = []
        self.pins_saved = []
        self.pristine_result = []
        self.git = RunStub()
        inst = stack.installer

        monkeypatch.setattr(inst, "_run", self.git)
        monkeypatch.setattr(inst, "find_python", lambda: "/py/3.12")
        monkeypatch.setattr(inst, "ensure_runtime_shims", lambda p: False)
        monkeypatch.setattr(inst, "_download",
                            lambda url, dest, **kw: Path(dest).write_bytes(b""))

        if not real_checkout:
            def fake_checkout(comp, ref, force=False):
                self.order.append("checkout:" + comp)
                self.checkouts.append((comp, ref, force))
                dest = inst.DESTS[comp]
                dest.mkdir(parents=True, exist_ok=True)
                inst._ref_marker(comp).write_text(str(ref) + "\n")
                return SHA
            monkeypatch.setattr(inst, "checkout", fake_checkout)

        def fake_create_venv():
            self.order.append("create_venv")
            p = stack.paths.VENV_PY
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
        monkeypatch.setattr(inst, "create_venv", fake_create_venv)

        for name, tag in (("install_requirements", "install_requirements"),
                          ("test_libsodium", "test_libsodium"),
                          ("test_chelper_build", "test_chelper_build")):
            def step(_n=tag):
                self.order.append(_n)
            monkeypatch.setattr(inst, name, step)

        def fake_fetch_webui(comp, tag, dest):
            self.order.append("fetch:" + comp)
            self.fetches.append((comp, tag))
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "index.html").write_text("<html>%s %s</html>" % (comp, tag))
        monkeypatch.setattr(inst, "fetch_webui", fake_fetch_webui)

        def fake_ensure_configs():
            self.order.append("ensure_configs")
            return []
        monkeypatch.setattr(stack.configgen, "ensure_configs",
                            fake_ensure_configs)

        def fake_verify_pristine(component=None):
            self.order.append("verify_pristine")
            return list(self.pristine_result)
        monkeypatch.setattr(inst, "verify_pristine", fake_verify_pristine)

        def fake_ensure_shaper(**kw):
            self.order.append("shaper")
            self.shaper_kwargs = kw
            return True, []
        monkeypatch.setattr(stack.shaper, "ensure_shaper", fake_ensure_shaper)

        real_save = inst.save_pins

        def spy_save(pins):
            self.pins_saved.append(dict(pins))
            return real_save(pins)
        monkeypatch.setattr(inst, "save_pins", spy_save)

        # a stray urlopen would mean the harness leaked real network access
        def _no_network(*a, **k):
            raise AssertionError("setup() reached the network")
        monkeypatch.setattr(inst.urllib.request, "urlopen", _no_network)

    # helpers -----------------------------------------------------------
    def lock(self, payload):
        d = self.stack.paths.LOCKFILE.parent
        d.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, (bytes, bytearray)):
            self.stack.paths.LOCKFILE.write_bytes(payload)
        elif isinstance(payload, str):
            self.stack.paths.LOCKFILE.write_text(payload)
        else:
            self.stack.paths.LOCKFILE.write_text(json.dumps(payload))

    def run_setup(self, **kw):
        kw.setdefault("verbose", False)
        return self.stack.installer.setup(**kw)

    def saved(self):
        return json.loads(self.stack.paths.LOCKFILE.read_text())


@pytest.fixture
def harness(stack, monkeypatch):
    return SetupHarness(stack, monkeypatch)


@pytest.fixture
def real_checkout_harness(stack, monkeypatch):
    """checkout() itself runs; only the subprocess layer is faked."""
    return SetupHarness(stack, monkeypatch, real_checkout=True)


# --------------------------------------------------------------------------
# pins: the shape setup() needs
# --------------------------------------------------------------------------
def test_missing_lockfile_setup_uses_the_shipped_pins(harness):
    pins = harness.run_setup()
    defaults = harness.stack.versions.DEFAULT_PINS
    for comp in ("klipper", "moonraker", "mainsail", "fluidd"):
        assert pins[comp] == defaults[comp]
    assert harness.saved()["klipper"] == pins["klipper"]


def test_setup_fetches_exactly_the_pinned_refs(harness):
    harness.lock({"klipper": "v0.0.1", "moonraker": "v2.3.4",
                  "mainsail": "v9.9.9", "fluidd": "v8.8.8"})
    harness.run_setup()
    assert [c for c, _, _ in harness.checkouts] == ["klipper", "moonraker"]
    assert harness.checkouts[0][1] == "v0.0.1"
    assert harness.checkouts[1][1] == "v2.3.4"
    assert harness.fetches == [("mainsail", "v9.9.9"), ("fluidd", "v8.8.8")]


def test_setup_records_the_resolved_sha_of_each_checkout(harness):
    harness.run_setup()
    saved = harness.saved()
    assert saved["klipper_sha"] == SHA
    assert saved["moonraker_sha"] == SHA


def test_setup_preserves_unrelated_lockfile_keys(harness):
    harness.lock({"ui": "mainsail", "custom": {"a": 1}, "klipper": "v1.2.3"})
    harness.run_setup()
    saved = harness.saved()
    assert saved["ui"] == "mainsail"
    assert saved["custom"] == {"a": 1}
    assert saved["klipper"] == "v1.2.3"


def test_setup_step_order_is_dependency_correct(harness):
    harness.run_setup()
    order = harness.order
    assert order[0] == "checkout:klipper"
    assert order.index("checkout:moonraker") < order.index("create_venv")
    assert order.index("create_venv") < order.index("install_requirements")
    assert order.index("install_requirements") < order.index("test_libsodium")
    assert order.index("test_libsodium") < order.index("fetch:mainsail")
    assert order.index("fetch:fluidd") < order.index("ensure_configs")
    assert order.index("ensure_configs") < order.index("test_chelper_build")
    assert order.index("test_chelper_build") < order.index("verify_pristine")
    assert order.index("verify_pristine") < order.index("shaper")


def test_prerequisites_gate_every_destructive_step(stack, harness, monkeypatch):
    """check_prereqs() must run before anything is fetched or written."""
    def boom():
        raise SystemExit("git missing")
    monkeypatch.setattr(stack.installer, "check_prereqs", boom)
    with pytest.raises(SystemExit):
        harness.run_setup()
    assert harness.order == [], "setup fetched things despite failing prereqs"


def test_a_failing_checkout_aborts_the_rest_of_setup(harness, monkeypatch):
    def boom(comp, ref, force=False):
        raise SystemExit("git clone failed")
    monkeypatch.setattr(harness.stack.installer, "checkout", boom)
    with pytest.raises(SystemExit):
        harness.run_setup()
    assert harness.order == [], "setup continued past a failed checkout"
    assert "create_venv" not in harness.order
    assert harness.fetches == []


def test_a_failing_venv_aborts_before_the_uis_are_downloaded(
        harness, monkeypatch):
    def boom():
        raise SystemExit("no python")
    monkeypatch.setattr(harness.stack.installer, "create_venv", boom)
    with pytest.raises(SystemExit):
        harness.run_setup()
    assert harness.fetches == []
    assert "ensure_configs" not in harness.order


def test_setup_is_idempotent_for_the_lockfile(harness):
    harness.run_setup()
    first = harness.stack.paths.LOCKFILE.read_text()
    harness.run_setup()
    assert harness.stack.paths.LOCKFILE.read_text() == first


def test_setup_lockfile_round_trip_is_stable_and_newline_terminated(harness):
    harness.run_setup()
    raw = harness.stack.paths.LOCKFILE.read_text()
    assert raw.endswith("\n")
    assert isinstance(json.loads(raw), dict)


def test_setup_verbose_false_prints_nothing(harness, capfd):
    harness.run_setup(verbose=False)
    assert capfd.readouterr().out == ""


def test_setup_verbose_true_reports_the_pins(harness, capfd):
    harness.run_setup(verbose=True)
    out = capfd.readouterr().out
    assert SHA in out or "mainsail" in out


def test_setup_with_shaper_flags_is_forwarded(harness):
    harness.run_setup(with_shaper=True, install_python=False)
    assert harness.shaper_kwargs["with_shaper"] is True
    assert harness.shaper_kwargs["install_python"] is False


def test_setup_returns_the_pins_it_fetched(harness):
    pins = harness.run_setup()
    assert pins["klipper"] == SHA
    assert pins["klipper_sha"] == SHA
    assert pins["ui"] in ("mainsail", "fluidd")


def test_setup_never_reshapes_the_ui_pin(harness):
    harness.lock({"ui": "mainsail"})
    pins = harness.run_setup()
    assert pins["ui"] == "mainsail"
    assert harness.saved()["ui"] == "mainsail"


# --------------------------------------------------------------------------
# the corrupt-lockfile family, as setup() sees it
# --------------------------------------------------------------------------
CORRUPT = [
    pytest.param("", id="empty-file"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("{", id="lone-brace"),
    pytest.param('{"klipper": ', id="truncated-mid-object"),
    pytest.param('{"klipper": "a",}', id="trailing-comma"),
    pytest.param("not json", id="plain-text"),
    pytest.param('﻿{"klipper": "a"}', id="utf8-bom"),
    pytest.param(b'{"klipper": "\xff\xfe"}', id="invalid-utf8-bytes"),
    pytest.param(b'{"klipper": \x00}', id="nul-byte"),
    pytest.param('{"klipper": "a"}{"moonraker": "b"}',
                 id="two-objects-concatenated"),
]


@pytest.mark.parametrize("payload", CORRUPT)
def test_setup_recovers_from_a_corrupt_lockfile(harness, payload):
    harness.lock(payload)
    pins = harness.run_setup()
    defaults = harness.stack.versions.DEFAULT_PINS
    assert pins["klipper"] == defaults["klipper"]
    assert pins["moonraker"] == defaults["moonraker"]


NON_OBJECT = [
    pytest.param('["klipper", "v1"]', id="array-of-strings"),
    pytest.param('"v0.11.0"', id="bare-string"),
    pytest.param("42", id="bare-int"),
    pytest.param("null", id="bare-null"),
    pytest.param("true", id="bare-true"),
    pytest.param('[{"klipper": "v1"}]', id="array-of-objects"),
]


@pytest.mark.parametrize("payload", NON_OBJECT)
def test_setup_recovers_from_a_non_object_lockfile(harness, payload):
    harness.lock(payload)
    pins = harness.run_setup()
    assert pins["klipper"] == harness.stack.versions.DEFAULT_PINS["klipper"]


def test_setup_tolerates_an_empty_array_lockfile(harness):
    """Documented non-bug: `[]` parses, merges to nothing, and repairs itself."""
    harness.lock("[]")
    pins = harness.run_setup()
    assert pins["klipper"] == harness.stack.versions.DEFAULT_PINS["klipper"]
    assert isinstance(harness.saved(), dict)


BAD_PIN_VALUES = [
    pytest.param(None, id="json-null"),
    pytest.param(42, id="json-int"),
    pytest.param(["v1"], id="json-list"),
    pytest.param({"tag": "v1"}, id="json-object"),
    pytest.param(True, id="json-bool"),
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("v1\nv2", id="embedded-newline"),
]


@pytest.mark.parametrize("value", BAD_PIN_VALUES)
def test_setup_refuses_or_repairs_a_bogus_pin_value(harness, value):
    harness.lock({"klipper": value})
    try:
        pins = harness.run_setup()
    except SystemExit:
        return                          # a clean refusal is acceptable
    assert pins["klipper"] != value, (
        "setup() accepted a bogus pin and saved it back")
    assert harness.saved()["klipper"] != value


def test_setup_repairs_a_lockfile_that_only_adds_unknown_keys(harness):
    """Extra keys are not corruption: they must survive, and pins still work."""
    harness.lock({"made_up": "x", "klipper": SHA})
    pins = harness.run_setup()
    assert pins["made_up"] == "x"
    assert pins["klipper"] == SHA
    assert harness.saved()["made_up"] == "x"


def test_setup_rereads_the_lockfile_it_just_wrote(harness):
    harness.lock({"moonraker": "v0.11.0"})
    harness.run_setup()
    assert harness.saved()["moonraker"] == "v0.11.0"
    assert ("moonraker", "v0.11.0", False) in harness.checkouts


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_setup_fails_before_any_destructive_step_when_home_is_unwritable(
        harness, monkeypatch):
    """A read-only ~/.klipperformac must not leave a half-installed tree."""
    monkeypatch.setattr(harness.stack.installer, "check_prereqs",
                        lambda strict=True: [])
    home = harness.stack.paths.APP_HOME
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(str(home), stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(OSError):
            harness.run_setup()
        assert harness.order == [], "setup() fetched into an unwritable home"
        assert harness.checkouts == []
    finally:
        os.chmod(str(home), 0o755)


def test_setup_never_runs_a_command_outside_the_pinned_toolchain(
        real_checkout_harness):
    """A lockfile must not be able to steer setup() into an arbitrary binary."""
    real_checkout_harness.lock({"klipper": SHA, "moonraker": "v0.11.0"})
    real_checkout_harness.run_setup()
    for argv, _ in real_checkout_harness.git.calls:
        assert argv[0] in ("git", "/py/3.12"), argv
    assert not any(";" in a or "|" in a or "&&" in a
                   for a in real_checkout_harness.git.joined_all())


def test_setup_with_a_pinned_sha_fetches_the_commit_not_a_branch(
        real_checkout_harness):
    real_checkout_harness.lock({"klipper": SHA, "moonraker": "v0.11.0"})
    real_checkout_harness.run_setup()
    joined = real_checkout_harness.git.joined_all()
    assert any("fetch --quiet --depth 1 origin " + SHA in j for j in joined)
    assert not any("--branch " + SHA in j for j in joined)
    assert any("clone --quiet --depth 1 --branch v0.11.0" in j for j in joined)


def test_setup_git_failures_do_not_write_a_lying_ref_marker(
        real_checkout_harness, monkeypatch):
    """A marker says 'this exact ref is on disk'; it must not survive a failure.

    Relabelled when the fetch failure became a CLI user error: checkout() now
    stages the clone outside the checkout and raises SystemExit, so a failed
    `git clone`/`git fetch` reaches cmd_setup as a one-line error instead of a
    subprocess.CalledProcessError traceback (see
    test_a_failed_fetch_must_not_destroy_the_existing_checkout for the tree
    that must survive). The marker assertion -- the point of this test -- is
    unchanged.
    """
    monkeypatch.setattr(real_checkout_harness.stack.installer, "_run",
                        RunStub(fail_on=("clone", "fetch")))
    with pytest.raises(SystemExit):
        real_checkout_harness.run_setup()
    for comp in ("klipper", "moonraker"):
        marker = real_checkout_harness.stack.installer._ref_marker(comp)
        assert not marker.exists(), "%s marked as fetched after a failed fetch" % comp


# --------------------------------------------------------------------------
# verify_pristine's verdict must gate setup's success
# --------------------------------------------------------------------------
def test_setup_fails_when_upstream_is_not_pristine(harness):
    harness.pristine_result = [("klipper", " M klippy/klippy.py")]
    with pytest.raises(SystemExit) as exc:
        harness.run_setup()
    assert exc.value.code not in (0, None)


def test_setup_still_creates_the_config_layer_before_the_build_test(harness):
    harness.run_setup()
    assert "ensure_configs" in harness.order


# --------------------------------------------------------------------------
# tripwire: the harness really is offline
# --------------------------------------------------------------------------
def test_harness_leaves_the_live_home_untouched(stack, harness):
    live = Path(os.path.expanduser("~/.klipperformac"))
    before = set(live.iterdir()) if live.exists() else None
    harness.run_setup()
    after = set(live.iterdir()) if live.exists() else None
    assert before == after, "setup() created files in the real ~/.klipperformac"


def test_harness_never_created_a_real_venv_or_checkout(stack, harness):
    harness.run_setup()
    assert (stack.paths.VENV_PY).parent.is_dir()
    assert stack.installer.DESTS["klipper"].is_dir()
    assert not (stack.installer.DESTS["klipper"] / ".git").exists()
    assert not any(p.name.endswith(".zip") for p in
                   Path(__file__).parent.iterdir())

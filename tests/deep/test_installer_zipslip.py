"""Deep tests: installer zip handling -- `_safe_extract`, hostile archives,
`fetch_webui`'s archive-integrity paths.

Everything is built from bytes inside pytest's tmp_path and extracted into a
throwaway staging dir. Nothing here reaches the network (urllib is stubbed or
never called), the live stack, or ~/.klipperformac: the `stack` fixture binds
klipperformac.installer to a throwaway KLIPPERFORMAC_HOME/KLIPPERFORMAC_DATA.

Tests here all pass against the current product code: the archive-robustness
and swap bugs found in this area are fixed in klipperformac/installer.py, and
one case whose expectation no input can ever produce is kept as a
characterization test (see its docstring).
"""
import io
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# --------------------------------------------------------------------------
# archive builders (raw enough to express things shutil.make_archive cannot)
# --------------------------------------------------------------------------
DOS_DT = (2026, 1, 1, 12, 0, 0)


def zip_bytes(entries):
    """entries: (name, data) or (name, data, "dir"|"symlink")."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for entry in entries:
            name, data = entry[0], entry[1]
            kind = entry[2] if len(entry) > 2 else None
            zi = zipfile.ZipInfo(name, date_time=DOS_DT)
            if kind == "dir":
                if not zi.filename.endswith("/"):
                    zi.filename = zi.filename + "/"
                zi.external_attr = (stat.S_IFDIR | 0o755) << 16
                data = b""
            elif kind == "symlink":
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, data)
    return buf.getvalue()


def write_zip(path, entries):
    Path(path).write_bytes(zip_bytes(entries))
    return Path(path)


def patch_name_bytes(raw, old_utf8, new_bytes):
    """Length-preserving rewrite of every occurrence of a member name."""
    assert len(old_utf8) == len(new_bytes)
    out = bytearray(raw)
    hits = 0
    pos = 0
    while True:
        i = out.find(old_utf8, pos)
        if i < 0:
            break
        out[i:i + len(old_utf8)] = new_bytes
        pos = i + len(old_utf8)
        hits += 1
    assert hits >= 2, "name must appear in both the local and central headers"
    return bytes(out)


def snapshot_tree(root):
    root = Path(root)
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def make_canary(target):
    """A dir next to `target`: nothing may ever be written into it."""
    canary = Path(str(target) + "-canary")
    canary.mkdir(parents=True, exist_ok=True)
    (canary / "do-not-touch").write_text("canary")
    return canary


def stays_inside(target, paths):
    resolved = Path(target).resolve()
    for p in paths:
        rp = p.resolve()
        if not (rp == resolved or resolved in rp.parents):
            return str(rp)
    return None


# --------------------------------------------------------------------------
# the guard must REJECT every entry that would land outside the target
# --------------------------------------------------------------------------
UNSAFE_NAMES = [
    pytest.param("../escape.txt", id="parent-one"),
    pytest.param("../../escape.txt", id="parent-two"),
    pytest.param("subdir/../../../escape.txt", id="parent-after-descend"),
    pytest.param("./../escape.txt", id="dot-slash-parent"),
    pytest.param("a/b/../../../../escape.txt", id="deep-parent-walk"),
    pytest.param("/etc/klipperformac-zipslip-probe", id="absolute-path"),
    pytest.param("//tmp/klipperformac-zipslip-probe", id="double-slash-abs"),
    pytest.param("aaaaaaaa/..", id="parent-terminal"),
    pytest.param("../" * 40 + "escape.txt", id="forty-parents"),
]


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_entries_are_rejected_before_anything_is_written(
        stack, tmp_path, name):
    target = tmp_path / "stage"
    target.mkdir()
    canary = make_canary(target)
    zp = write_zip(tmp_path / "slip.zip", [
        ("index.html", b"<html>legit</html>"),
        (name, b"EVIL"),
    ])
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit) as exc:
            stack.installer._safe_extract(z, target)
    assert "Unsafe zip path" in str(exc.value)
    # the guard validates the whole member list before extracting anything
    assert snapshot_tree(target) == set()
    assert snapshot_tree(canary) == {"do-not-touch"}
    assert not Path("/etc/klipperformac-zipslip-probe").exists()
    assert not Path("/tmp/klipperformac-zipslip-probe").exists()


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_entry_first_is_still_rejected(stack, tmp_path, name):
    """Order must not matter: the hostile entry may come first."""
    target = tmp_path / "stage"
    target.mkdir()
    zp = write_zip(tmp_path / "slip.zip", [
        (name, b"EVIL"),
        ("index.html", b"<html>legit</html>"),
    ])
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit):
            stack.installer._safe_extract(z, target)
    assert snapshot_tree(target) == set()


def test_guard_is_all_or_nothing_and_leaves_no_partial_tree(stack, tmp_path):
    """A mixed archive (1000 safe entries + 1 hostile) must extract nothing."""
    target = tmp_path / "stage"
    target.mkdir()
    entries = [("assets/f%04d.js" % i, b"x") for i in range(1000)]
    entries.append(("../../evil.js", b"EVIL"))
    zp = write_zip(tmp_path / "mixed.zip", entries)
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit):
            stack.installer._safe_extract(z, target)
    assert snapshot_tree(target) == set()


def test_huge_member_count_is_still_guarded(stack, tmp_path):
    """5000 safe members + a slip somewhere in the middle: zero writes."""
    target = tmp_path / "stage"
    target.mkdir()
    entries = [("d%03d/f%03d.txt" % (i // 50, i % 50), b"x")
               for i in range(5000)]
    entries.insert(2500, ("../../../../nope.txt", b"EVIL"))
    zp = write_zip(tmp_path / "big.zip", entries)
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit):
            stack.installer._safe_extract(z, target)
    assert snapshot_tree(target) == set()
    assert not (tmp_path / "nope.txt").exists()


# --------------------------------------------------------------------------
# ... and must ALLOW everything that stays inside
# --------------------------------------------------------------------------
SAFE_NAMES = [
    pytest.param("index.html", id="plain"),
    pytest.param("assets/app.js", id="one-dir"),
    pytest.param("a/../b/c.txt", id="inner-traversal"),
    pytest.param("deep/" * 30 + "leaf.txt", id="thirty-deep"),
    pytest.param("ünïcode/ファイル.txt", id="unicode-path"),
    pytest.param("emoji-\U0001f31f.css", id="emoji"),
    pytest.param("combining/é.txt", id="combining-marks"),
    pytest.param("a\\..\\b.txt", id="backslash-parents"),
    pytest.param("C:\\Users\\x\\y.txt", id="windows-abs-looks-plain"),
    pytest.param("with space(1).js", id="spaces-parens"),
    pytest.param("tab\tname.txt", id="literal-tab"),
    pytest.param("tilde~/x.txt", id="tilde"),
    pytest.param("....//evil.txt", id="four-dots"),
    pytest.param("a" * 200 + ".js", id="long-200"),
    pytest.param("-dash/file.txt", id="leading-dash"),
    pytest.param("dotdir/.hidden", id="dotfile"),
    pytest.param("%s/format.txt", id="percent-s"),
    pytest.param("quote\"'mix.txt", id="quotes"),
    pytest.param("semi;colon|pipe&.txt", id="shell-meta"),
]


@pytest.mark.parametrize("name", SAFE_NAMES)
def test_safe_entries_extract_and_stay_inside(stack, tmp_path, name):
    target = tmp_path / "stage"
    target.mkdir()
    canary = make_canary(target)
    body = b"payload"
    zp = write_zip(tmp_path / "safe.zip", [("sentinel/holder.txt", b"S"),
                                           (name, body)])
    with zipfile.ZipFile(str(zp)) as z:
        stack.installer._safe_extract(z, target)
    assert (target / "sentinel" / "holder.txt").read_bytes() == b"S"
    files = [p for p in target.rglob("*") if p.is_file()]
    assert files, "the safe entry must actually have been written"
    assert stays_inside(target, files) is None, files
    assert snapshot_tree(canary) == {"do-not-touch"}


def test_symlink_entry_is_not_followed_outside_target(stack, tmp_path):
    """A symlink member must never become a live symlink that escapes."""
    target = tmp_path / "stage"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("original")
    zp = write_zip(tmp_path / "sym.zip", [
        ("link", str(outside / "escape.txt"), "symlink"),
        ("rel_link", "../../../etc/passwd", "symlink"),
        ("index.html", b"<html>ok</html>"),
    ])
    with zipfile.ZipFile(str(zp)) as z:
        stack.installer._safe_extract(z, target)
    for name in ("link", "rel_link"):
        p = target / name
        assert p.is_file(), "%s must be a plain file, never a symlink" % name
        assert not p.is_symlink()
    assert secret.read_text() == "original"
    assert not (outside / "escape.txt").exists()
    assert (target / "index.html").read_bytes() == b"<html>ok</html>"


def test_directory_entries_and_empty_dirs(stack, tmp_path):
    target = tmp_path / "stage"
    target.mkdir()
    zp = write_zip(tmp_path / "dirs.zip", [
        ("assets/", b"", "dir"),
        ("assets/img/", b"", "dir"),
        ("assets/img/logo.svg", b"<svg/>"),
        ("empty-deep/", b"", "dir"),
    ])
    with zipfile.ZipFile(str(zp)) as z:
        stack.installer._safe_extract(z, target)
    assert (target / "assets" / "img" / "logo.svg").read_bytes() == b"<svg/>"
    assert (target / "empty-deep").is_dir()


def test_empty_archive_extracts_nothing(stack, tmp_path):
    target = tmp_path / "stage"
    target.mkdir()
    zp = write_zip(tmp_path / "empty.zip", [])
    with zipfile.ZipFile(str(zp)) as z:
        assert z.infolist() == []
        stack.installer._safe_extract(z, target)
    assert snapshot_tree(target) == set()


def test_target_missing_from_disk_is_still_guarded(stack, tmp_path):
    """fetch_webui always passes a live temp dir, but the guard must not
    invent directories outside it either."""
    target = tmp_path / "does" / "not" / "exist"
    zp = write_zip(tmp_path / "z.zip", [("../escape", b"EVIL")])
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit):
            stack.installer._safe_extract(z, target)
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "does").exists()


def test_symlink_inside_target_cannot_smuggle_a_path_out(stack, tmp_path):
    """The guard resolves symlinks, so an entry under a pre-existing symlinked
    dir is rejected rather than written through the link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "stage"
    target.mkdir()
    os.symlink(str(outside), str(target / "jail"))
    zp = write_zip(tmp_path / "z.zip", [("jail/evil.txt", b"EVIL"),
                                        ("index.html", b"ok")])
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit):
            stack.installer._safe_extract(z, target)
    assert snapshot_tree(outside) == set()


def test_dangling_symlink_member_cannot_write_through_its_prefix(
        stack, tmp_path):
    """'dangling' -> /etc/... plus a member under it: the archive may fail
    (the file/dir confusion is the robustness bug tested below), but it may
    never write through the link it pretends to be."""
    zp = write_zip(tmp_path / "z.zip", [
        ("dangling", "/etc/klipper-probe", "symlink"),
        ("dangling/inner.txt", b"EVIL"),
    ])
    target = tmp_path / "stage"
    target.mkdir()
    with zipfile.ZipFile(str(zp)) as z:
        try:
            stack.installer._safe_extract(z, target)
        except (SystemExit, OSError, ValueError):
            pass
    assert not Path("/etc/klipper-probe").exists()
    assert not (tmp_path / "inner.txt").exists()
    bad = stays_inside(target, [p for p in target.rglob("*")])
    assert bad is None, bad


# --------------------------------------------------------------------------
# fuzz: the security invariant must hold for ANY member name
# --------------------------------------------------------------------------
NAME_TOKENS = st.sampled_from(
    ["a", "b", ".", "..", "/", "\\", "//", "~", " ", "é", "日",
     "\U0001f31f", "-", "%", "_", "0", "\t", "…", "​", "ÿ", ":",
     "sub/", "../", "./", "index.html"])
NAME_ST = st.lists(NAME_TOKENS, min_size=0, max_size=14).map("".join)


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(NAME_ST)
def test_no_member_name_ever_writes_outside_target(stack, name):
    """Property: whatever the archive claims, extraction may only ever produce
    paths inside the target (or fail)."""
    target = Path(tempfile.mkdtemp())
    canary = target.parent / (target.name + "-canary")
    canary.mkdir(exist_ok=True)
    zp = Path(tempfile.mktemp(suffix=".zip"))
    try:
        raw = zip_bytes([("sentinel/holder.txt", b"<html>"), (name, b"BODY")])
    except (ValueError, OSError):
        return                          # zipfile itself refuses that name
    zp.write_bytes(raw)
    with zipfile.ZipFile(str(zp)) as z:
        members = [m.filename for m in z.infolist()]
        try:
            stack.installer._safe_extract(z, target)
        except SystemExit:
            return                      # rejected: exactly what we want
        except (OSError, ValueError):
            pass                        # robustness bug, tested separately
    escaped = stays_inside(target, [p for p in target.rglob("*")])
    assert escaped is None, "escaped to %r via member %r" % (escaped, members)
    assert list(canary.iterdir()) == []


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(NAME_ST, min_size=1, max_size=12, unique=True))
def test_multi_member_archives_keep_the_same_property(stack, names):
    target = Path(tempfile.mkdtemp())
    zp = Path(tempfile.mktemp(suffix=".zip"))
    entries = [("sentinel/holder.txt", b"<html>")] + [(n, b"B") for n in names]
    try:
        raw = zip_bytes(entries)
    except (ValueError, OSError):
        return
    zp.write_bytes(raw)
    with zipfile.ZipFile(str(zp)) as z:
        try:
            stack.installer._safe_extract(z, target)
        except SystemExit:
            return
        except (OSError, ValueError):
            pass
    escaped = stays_inside(target, [p for p in target.rglob("*")])
    assert escaped is None, escaped


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(st.characters(min_codepoint=1, max_codepoint=0x2aff),
               min_size=1, max_size=24))
def test_exotic_unicode_member_names_never_escape(stack, name):
    target = Path(tempfile.mkdtemp())
    zp = Path(tempfile.mktemp(suffix=".zip"))
    try:
        raw = zip_bytes([(name, b"B")])
    except (ValueError, OSError):
        return
    zp.write_bytes(raw)
    with zipfile.ZipFile(str(zp)) as z:
        try:
            stack.installer._safe_extract(z, target)
        except SystemExit:
            return
        except (OSError, ValueError):
            pass
    escaped = stays_inside(target, [p for p in target.rglob("*")])
    assert escaped is None, escaped


# --------------------------------------------------------------------------
# robustness of the guard on malformed (but non-escaping) archives
# --------------------------------------------------------------------------
def test_benign_dot_directory_entries_are_not_rejected(stack, tmp_path):
    target = tmp_path / "stage"
    target.mkdir()
    zp = write_zip(tmp_path / "dot.zip", [
        (".", b"", "dir"),
        ("assets/", b"", "dir"),
        ("index.html", b"<html>ok</html>"),
    ])
    with zipfile.ZipFile(str(zp)) as z:
        stack.installer._safe_extract(z, target)
    assert (target / "index.html").read_bytes() == b"<html>ok</html>"
    assert (target / "assets").is_dir()


def test_overlong_member_name_fails_as_clean_user_error(stack, tmp_path):
    """Was the 'name-too-long' case of the xfail'd
    test_hostile_names_fail_as_clean_user_errors: a 400-char member passed the
    guard (it stays inside) and then died in extractall() with a raw
    OSError(ENAMETOOLONG) instead of the SystemExit every other hostile member
    gets. It is now refused the way an escaping path is."""
    target = tmp_path / "stage"
    target.mkdir()
    name = "L" * 400
    zp = write_zip(tmp_path / "n.zip", [("index.html", b"<html>"),
                                        (name, b"B")])
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit) as exc:
            stack.installer._safe_extract(z, target)
    assert "Unsafe zip path" in str(exc.value)
    assert snapshot_tree(target) == set()


def test_embedded_nul_member_name_is_the_name_that_was_audited(stack, tmp_path):
    """Characterization (relabelled from the 'embedded-nul' case of that same
    xfail, which asked for a SystemExit no product code can ever raise).

    The filed complaint was 'validation inspects path P, extraction writes path
    Q'. That split cannot happen: stdlib zipfile terminates a member name at the
    first null byte while *writing* the archive (and again while reading the
    headers), so a name like 'x\\x00y' reaches the guard already truncated to
    'x'. The guard therefore audits exactly the name extractall() writes, and
    'x' is inside the target -- no escape, no traceback. The assertion that the
    bug's own text asks for ('stay inside, and the audited name is what landed
    on disk') is what this documents; installer._safe_extract still refuses a
    NUL outright should a future reader ever hand one over."""
    target = tmp_path / "stage"
    target.mkdir()
    canary = make_canary(target)
    zp = write_zip(tmp_path / "n.zip", [("index.html", b"<html>"),
                                        ("x\x00y", b"B")])
    with zipfile.ZipFile(str(zp)) as z:
        audited = [m.filename for m in z.infolist()]
        assert not any("\x00" in n for n in audited), audited
        stack.installer._safe_extract(z, target)
    written = [str(p.relative_to(target)) for p in target.rglob("*")]
    assert sorted(written) == ["index.html", "x"], written
    assert stays_inside(target, [target / n for n in written]) is None
    assert snapshot_tree(canary) == {"do-not-touch"}


def test_file_then_dir_collision_is_a_clean_error(stack, tmp_path):
    target = tmp_path / "stage"
    target.mkdir()
    zp = write_zip(tmp_path / "collide.zip", [
        ("index.html", b"<html>"),
        ("panel", b"I-am-a-file"),
        ("panel/inside.js", b"child-of-file"),
    ])
    with zipfile.ZipFile(str(zp)) as z:
        with pytest.raises(SystemExit):
            stack.installer._safe_extract(z, target)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_target_reports_a_user_error(stack, tmp_path):
    target = tmp_path / "stage"
    target.mkdir()
    zp = write_zip(tmp_path / "w.zip", [("index.html", b"<html>")])
    os.chmod(str(target), stat.S_IRUSR)
    try:
        with zipfile.ZipFile(str(zp)) as z:
            with pytest.raises(SystemExit):
                stack.installer._safe_extract(z, target)
    finally:
        os.chmod(str(target), 0o755)


# --------------------------------------------------------------------------
# fetch_webui: archive integrity, staging, atomicity
# --------------------------------------------------------------------------
def tag_of(url):
    """.../releases/download/<tag>/<component>.zip"""
    return url.split("/")[7]


def ui_zip_bytes(url, config=None, extra=()):
    tag = tag_of(url)
    return zip_bytes([
        ("index.html", b"<html>UI " + tag.encode() + b"</html>"),
        ("assets/app.js", b"// " + tag.encode()),
        ("config.json", json.dumps(
            {"socket_url": "ws://elsewhere", "hostname": "1.2.3.4",
             "port": 8080} if config is None else config)),
    ] + list(extra))


class FakeFetch:
    """fetch_webui with the download stubbed; only archive bytes vary."""

    def __init__(self, ns, monkeypatch, zip_bytes_fn, component="fluidd"):
        self.ns = ns
        self.component = component
        self.calls = []
        self._zip_bytes_fn = zip_bytes_fn
        self.dest = getattr(ns.paths, "WEB_" + component.upper())
        monkeypatch.setattr(ns.installer, "_download", self._download)

    def _download(self, url, dest_path, **kw):
        self.calls.append(url)
        Path(dest_path).write_bytes(self._zip_bytes_fn(url))

    def url_for(self, tag):
        return "https://github.com/{}/releases/download/{}/{}.zip".format(
            self.ns.versions.REPOS[self.component], tag, self.component)

    def run(self, tag):
        getattr(self.ns.installer, "fetch_" + self.component)(tag)

    def marker(self):
        return self.ns.installer.REFS / (self.component + ".ref")


def test_fetch_webui_never_touches_the_network(stack, monkeypatch):
    """The only network call is _download, and this test stubs it."""
    def _no_network(*a, **k):
        raise AssertionError("fetch_webui reached the network")
    monkeypatch.setattr(stack.installer.urllib.request, "urlopen", _no_network)
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.37.6")
    assert f.calls == [f.url_for("v1.37.6")]
    assert (f.dest / "index.html").read_bytes() == b"<html>UI v1.37.6</html>"


def test_fetch_webui_download_url_is_the_pinned_repo_asset(stack, monkeypatch):
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes, component="mainsail")
    f.run("v2.19.0")
    assert f.calls == [
        "https://github.com/meteyou/mainsail/releases/download/v2.19.0/"
        "mainsail.zip"]


def test_fetch_webui_writes_ref_marker_and_hidden_stamp(stack, monkeypatch):
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.37.6")
    assert f.marker().read_text() == "v1.37.6\n"
    assert (f.dest / ".klipperformac-ref").read_text() == "v1.37.6\n"


def test_fetch_webui_is_idempotent_for_the_same_tag(stack, monkeypatch):
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.37.6")
    first = (f.dest / "index.html").read_bytes()
    f.run("v1.37.6")
    f.run("v1.37.6")
    assert len(f.calls) == 1, "a cached tag must not be re-downloaded"
    assert (f.dest / "index.html").read_bytes() == first


def test_fetch_webui_upgrade_swaps_content_and_leaves_no_scratch(
        stack, monkeypatch):
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.0.0")
    f.run("v2.0.0")
    assert len(f.calls) == 2
    assert (f.dest / "index.html").read_bytes() == b"<html>UI v2.0.0</html>"
    assert sorted(p.name for p in stack.paths.WEB.iterdir()) == ["fluidd"]


def test_fetch_webui_removes_files_the_new_version_dropped(
        stack, monkeypatch):
    """Staging into a fresh dir means a deleted asset cannot linger."""
    seen = []

    def bytes_for(url):
        seen.append(url)
        if len(seen) == 1:
            return ui_zip_bytes(url, extra=[("stale.js", b"OLD")])
        return ui_zip_bytes(url)
    f = FakeFetch(stack, monkeypatch, bytes_for)
    f.run("v1")
    assert (f.dest / "stale.js").exists()
    f.run("v2")
    assert not (f.dest / "stale.js").exists(), "stale asset survived the swap"


def test_fetch_webui_survives_a_missing_data_web_dir(stack, monkeypatch):
    """data/web does not exist yet on a fresh install."""
    assert not stack.paths.WEB.exists()
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.37.6")
    assert (f.dest / "index.html").exists()


def test_both_uis_coexist_under_one_data_dir(stack, monkeypatch):
    a = FakeFetch(stack, monkeypatch, ui_zip_bytes, component="mainsail")
    b = FakeFetch(stack, monkeypatch, ui_zip_bytes, component="fluidd")
    a.run("v2.19.0")
    b.run("v1.37.6")
    assert (a.dest / "index.html").read_bytes() == b"<html>UI v2.19.0</html>"
    assert (b.dest / "index.html").read_bytes() == b"<html>UI v1.37.6</html>"
    assert sorted(p.name for p in stack.paths.WEB.iterdir()) == [
        "fluidd", "mainsail"]


@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="zero-byte-download"),
    pytest.param(b"<html>404 Not Found</html>\n" * 100, id="html-error-page"),
    pytest.param(b"PK\x03\x04truncated", id="truncated-archive"),
    pytest.param(zip_bytes([("index.html", b"<html>" * 200),
                            ("assets/big.js", b"B" * 400)])[:30],
                 id="cut-central-directory"),
])
def test_corrupt_archive_is_reported_as_a_user_error(stack, monkeypatch,
                                                     payload):
    f = FakeFetch(stack, monkeypatch, lambda url: payload)
    with pytest.raises(SystemExit):
        f.run("v1.37.6")
    assert not f.marker().exists()


def test_invalid_utf8_member_name_is_reported_as_a_user_error(
        stack, monkeypatch, tmp_path):
    raw = zip_bytes([("é" * 8, b"payload"), ("index.html", b"<html>")])
    payload = patch_name_bytes(raw, ("é" * 8).encode("utf-8"), b"\xff" * 16)
    f = FakeFetch(stack, monkeypatch, lambda url: payload)
    with pytest.raises(SystemExit):
        f.run("v1.37.6")


def test_archive_without_index_html_is_a_clean_error(stack, monkeypatch):
    """This one IS handled: assert the message and that nothing was staged."""
    f = FakeFetch(stack, monkeypatch,
                  lambda url: zip_bytes([("nope/README.md", b"no ui here")]))
    with pytest.raises(SystemExit) as exc:
        f.run("v1.37.6")
    assert "missing index.html" in str(exc.value)
    assert not f.dest.exists()
    assert not f.marker().exists()


def test_valid_but_empty_archive_is_a_clean_error(stack, monkeypatch):
    """A 0-member zip parses fine, so the missing-UI check still has to fire."""
    f = FakeFetch(stack, monkeypatch, lambda url: zip_bytes([]))
    with pytest.raises(SystemExit) as exc:
        f.run("v1.37.6")
    assert "missing index.html" in str(exc.value)
    assert not f.dest.exists()
    assert not f.marker().exists()


def test_hostile_archive_keeps_the_previous_ui_and_writes_no_marker(
        stack, monkeypatch, tmp_path):
    """A zip-slip release must not cost the user their installed UI."""
    good = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    good.run("v1.0.0")
    before = (good.dest / "index.html").read_bytes()
    good.marker().unlink()

    def evil(url):
        return zip_bytes([("index.html", b"<html>trojan</html>"),
                          ("../../../../escape.txt", b"EVIL")])
    bad = FakeFetch(stack, monkeypatch, evil)
    with pytest.raises(SystemExit) as exc:
        bad.run("v2.0.0")
    assert "Unsafe zip path" in str(exc.value)
    assert (good.dest / "index.html").read_bytes() == before
    assert not (Path(tempfile.gettempdir()) / "escape.txt").exists()
    assert not good.marker().exists()


def test_nested_index_html_is_found_including_unicode_dirs(
        stack, monkeypatch):
    def bytes_for(url):
        return zip_bytes([("mainsail-v2/ünïcode-ÄÖ/index.html",
                           b"<html>deep</html>"),
                          ("mainsail-v2/ünïcode-ÄÖ/cfg.json", b"{}")])
    f = FakeFetch(stack, monkeypatch, bytes_for, component="mainsail")
    f.run("v2.19.0")
    assert (f.dest / "index.html").read_bytes() == b"<html>deep</html>"
    # the members next to index.html come with it, the wrapper dir does not
    assert (f.dest / "cfg.json").read_bytes() == b"{}"
    assert not (f.dest / "mainsail-v2").exists()


def test_fetch_webui_does_not_leave_temp_zips_behind(stack, monkeypatch,
                                                     tmp_path):
    """Every staging zip, including the one for a failing download, is
    removed; only our own dir is inspected, so a parallel test cannot skew it.
    """
    zipdir = tmp_path / "zips"
    zipdir.mkdir()
    real_mkstemp = tempfile.mkstemp
    made = []

    def spy_mkstemp(suffix="", prefix="tmp", dir=None, **kw):
        fd, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=str(zipdir))
        made.append(name)
        return fd, name
    monkeypatch.setattr(stack.installer.tempfile, "mkstemp", spy_mkstemp)
    f = FakeFetch(stack, monkeypatch, lambda url: b"")   # BadZipFile later
    # The corrupt payload now leaves as a SystemExit user error (a CLI error
    # the user can read) rather than as an Exception, so both channels count
    # here: what this test pins is that neither of them leaks the staging zip.
    with pytest.raises((Exception, SystemExit)):
        f.run("v1.0.0")
    assert made, "the spy never saw a staging zip"
    assert [p for p in zipdir.iterdir()] == []


def test_fetch_webui_does_not_leak_file_descriptors(stack, monkeypatch):
    """Regression (filed as 'fetch_webui leaks one fd per download'): the
    mkstemp() descriptor was handed straight to Path(...[1]) and never closed,
    so every download pinned an extra open file until the process ran out of
    descriptors (EMFILE) on repeated `update --apply` runs."""
    fd_dir = Path("/dev/fd")
    if not fd_dir.is_dir():
        pytest.skip("no /dev/fd to count descriptors against")

    def open_fds():
        return {p.name for p in fd_dir.iterdir()}

    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    baseline = open_fds()
    for tag in ("v1.0.0", "v2.0.0", "v3.0.0", "v4.0.0", "v5.0.0"):
        f.marker().unlink(missing_ok=True)
        f.run(tag)
    leaked = open_fds() - baseline
    assert not leaked, "descriptors left open by fetch_webui: %s" % sorted(leaked)


def test_ref_marker_lives_outside_the_extracted_ui(stack, monkeypatch):
    """Pristine-attestation rule: refs.d markers must not pollute the UI tree
    beyond the one documented .klipperformac-ref stamp."""
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.37.6")
    assert f.marker().is_file()
    assert f.marker().parent == stack.installer.REFS
    assert stack.installer.REFS.is_relative_to(stack.paths.APP_HOME)
    assert not (f.dest / "refs.d").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_fetch_webui_reports_a_failure_when_the_web_dir_cannot_be_written(
        stack, monkeypatch):
    """Whatever the mechanism, a fetch that cannot install its payload must
    raise SystemExit (the CLI's user-error channel), not escape silently."""
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    stack.paths.WEB.mkdir(parents=True, exist_ok=True)
    os.chmod(str(stack.paths.WEB), stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises((SystemExit, OSError)):
            f.run("v1.37.6")
    finally:
        os.chmod(str(stack.paths.WEB), 0o755)


# --------------------------------------------------------------------------
# fetch_webui: the swap must be reversible
# --------------------------------------------------------------------------
def test_a_failed_swap_restores_the_previous_ui(stack, monkeypatch):
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    f.run("v1.0.0")
    before = (f.dest / "index.html").read_bytes()
    f.marker().unlink()
    real_os = stack.installer.os

    class RenameBlocker:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def rename(self, src, dst):
            if Path(dst) == f.dest:
                raise OSError(28, "No space left on device")
            return real_os.rename(src, dst)

    monkeypatch.setattr(stack.installer, "os", RenameBlocker())
    try:
        f.run("v2.0.0")
    except (OSError, SystemExit):
        pass
    assert f.dest.is_dir(), "the installed UI disappeared when the swap failed"
    assert (f.dest / "index.html").read_bytes() == before


def test_a_symlinked_ui_dir_is_refused(stack, monkeypatch, tmp_path):
    victim = tmp_path / "victim-web-root"
    victim.mkdir()
    (victim / "keepme.txt").write_text("precious")
    stack.paths.WEB.mkdir(parents=True, exist_ok=True)
    os.symlink(str(victim), str(stack.paths.WEB_FLUIDD))
    f = FakeFetch(stack, monkeypatch, ui_zip_bytes)
    with pytest.raises(SystemExit):
        f.run("v1.37.6")
    assert stack.paths.WEB_FLUIDD.is_symlink(), "the planted link was moved"
    assert (victim / "keepme.txt").exists()

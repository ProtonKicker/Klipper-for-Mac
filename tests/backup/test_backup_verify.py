"""`klipperformac backup verify` - the command that decides whether a backup can
be trusted, and the reason the format exists at all.

Three layers, each one a thing a tar check alone cannot prove:
  1. the gzip stream (every byte decompressed, CRC verified at the end),
  2. the tar structure and *every member name* (zip-slip, symlink/special
     members, names the layout has no root for),
  3. the content of the two files the stack boots from, parsed with our own
     readers - paths.read_config()'s decode chain plus the INI structure
     klippy/Moonraker read.
A backup that cannot restore has to say so, in one clean line, and exit 1.
"""
import gzip
import io
import json
import tarfile

import pytest

GOOD_PRINTER = "[printer]\nmax_velocity: 300\n\n[mcu]\nserial: /dev/a\n"
GOOD_MOONRAKER = "[server]\nhost: 127.0.0.1\nklippy_uds_address: /x/klippy.sock\n"


def valid_members(extra=()):
    """A complete, restorable archive body plus anything a test wants to add."""
    base = [("data/config/", "dir", b""),
            ("data/config/printer.cfg", "reg", GOOD_PRINTER),
            ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
            ("data/gcodes/", "dir", b""),
            ("data/gcodes/cube.gcode", "reg", "G28\n"),
            ("app/settings.json", "reg", '{"lan": false}')]
    return base + list(extra)


def put(bstack, tools, extra=(), manifest=None,
        name="klipperformac-backup-20260101-000000.tar.gz"):
    """Write a hand-built archive into DATA/backups. `extra` members are appended
    after the good ones, which is also what makes a duplicate name win the way
    tar itself decides."""
    blob = tools.build(valid_members(extra), manifest=manifest)
    return tools.write_backup(bstack.paths.DATA, name, blob)


def test_a_real_backup_verifies_ok(bstack, tools, capsys):
    path = bstack.backup.create()
    ok, lines = bstack.backup.verify(path.name)
    assert ok is True, lines
    assert any("gzip stream intact" in line for _good, line in lines)
    assert any("parses as a Klipper config" in line for _good, line in lines)
    assert any("parses as a Moonraker config" in line for _good, line in lines)


def test_verify_of_latest_is_the_default(bstack, tools):
    newest = None
    for _ in range(2):
        newest = bstack.backup.create()
    ok, lines = bstack.backup.verify()
    assert ok is True
    assert newest.name in [line for _good, line in lines][0]


def test_verify_names_the_archive_it_was_given(bstack, tools):
    path = put(bstack, tools)
    _ok, lines = bstack.backup.verify(path.name)
    assert path.name in lines[0][1]


def test_verify_reports_the_member_count_and_layout(bstack, tools):
    path = put(bstack, tools)
    ok, lines = bstack.backup.verify(path.name)
    assert ok is True
    assert any("2 folder(s) + 4 file(s)" in line for _good, line in lines)


# ------------------------------------------------------------- layer 1: gzip -

def test_verify_refuses_a_file_that_is_not_gzip(bstack, tools):
    """The `.tar.gz` extension is a promise, not evidence: rename a bare tar and
    it must be refused before anything is read as a member."""
    raw = tools.build(valid_members(), gz=False)
    path = tools.write_backup(bstack.paths.DATA,
                              "klipperformac-backup-20260101-000000.tar.gz", raw)
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False
    assert any("gzip" in line for _good, line in lines)


def test_verify_refuses_a_truncated_archive(bstack, tools):
    """A backup copied over a half-finished scp, or a killed Time Machine sync:
    the gzip CRC only fails at the end, which is exactly why the whole stream is
    read before anything is trusted."""
    path = bstack.backup.create()
    blob = path.read_bytes()
    path.write_bytes(blob[:int(len(blob) * 0.6)])
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False
    assert any("gzip" in line for _good, line in lines)


def test_verify_refuses_a_corrupted_gzip_payload(bstack, tools):
    path = bstack.backup.create()
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF  # flip a byte inside the compressed stream
    path.write_bytes(bytes(blob))
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False


def test_verify_accepts_a_gzip_with_no_tar_inside(bstack, tools):
    """Empty tar (no members at all) - structurally readable, restores nothing,
    and must not be advertised as a backup."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    blob = buf.getvalue()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(blob)
    path = tools.write_backup(bstack.paths.DATA,
                              "klipperformac-backup-20260101-000000.tar.gz",
                              out.getvalue())
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False
    assert any("no files at all" in line for _good, line in lines)


# ------------------------------------------------------ layer 2: member names -

HOSTILE = [
    ("absolute path", "/tmp/evil.cfg"),
    ("home-relative", "~/evil.cfg"),
    ("parent escape", "data/config/../../../../tmp/evil.cfg"),
    ("bare parent", "../evil.cfg"),
    ("escape inside data", "data/config/history/../../run/evil.cfg"),
    ("no known root", "etc/passwd"),
    ("root as a file", "data"),
    ("root as a file 2", "app"),
    ("hidden escape", "./../../x"),
]


@pytest.mark.parametrize("label,name", HOSTILE)
def test_verify_refuses_a_member_outside_the_layout(bstack, tools, label, name):
    """The zip-slip equivalent, and it is checked before a single byte is
    written - the same discipline installer._safe_extract() applies to the web UI
    zips."""
    path = put(bstack, tools, extra=[(name, "reg", "evil\n")])
    with pytest.raises(SystemExit) as exc:
        bstack.backup.verify(path.name)
    assert exc.value.code == 1
    assert not (bstack.paths.DATA / "evil.cfg").exists()


def test_verify_refuses_a_symlink_member(bstack, tools):
    """A symlink is the other half of zip-slip: it lands inside the target folder
    and points somewhere else. Our own create() never writes one, so one is
    proof of a hand-made or tampered archive - and a restore would have to either
    follow it or replace it, both of which are someone else's decision."""
    path = put(bstack, tools, extra=[("data/config/evil.cfg", "sym", b"",
                                      "/etc/passwd")])
    with pytest.raises(SystemExit) as exc:
        bstack.backup.verify(path.name)
    assert exc.value.code == 1
    assert not (bstack.paths.CONFIG / "evil.cfg").is_symlink()


def test_verify_refuses_a_hardlink_member(bstack, tools):
    path = put(bstack, tools, extra=[("data/config/evil.cfg", "lnk", b"",
                                      "data/gcodes/cube.gcode")])
    with pytest.raises(SystemExit):
        bstack.backup.verify(path.name)


def test_verify_refuses_a_fifo_member(bstack, tools):
    path = put(bstack, tools, extra=[("data/config/pipe", "fifo", b"")])
    with pytest.raises(SystemExit):
        bstack.backup.verify(path.name)


def test_verify_refuses_a_huge_member_name(bstack, tools):
    """A 100 KB member name is not a name: it cannot be written back (255 bytes
    per component) and it used to be able to blow up a listing."""
    huge = "data/config/" + ("A" * 10000) + ".cfg"
    path = put(bstack, tools, extra=[(huge, "reg", "x\n")])
    with pytest.raises(SystemExit):
        bstack.backup.verify(path.name)


def test_verify_accepts_a_long_but_legal_member_name(bstack, tools):
    """The guard is 255 BYTES per component, which is the filesystem's limit -
    not a made-up one, so a descriptive 200 character config name still restores.
    (A preset name is capped harder, at 100 characters, because it is typed.)"""
    name = "data/config/presets/" + ("voron" * 40)[:200] + ".cfg"
    assert len(name.split("/")[-1].encode()) < 255
    path = put(bstack, tools, extra=[(name, "reg", "[printer]\n")])
    ok, _lines = bstack.backup.verify(path.name)
    assert ok is True


def test_member_target_refuses_a_nul_byte(bstack):
    """Unit-level, because tar cannot store a NUL in a name at all (the header
    field is NUL-terminated) - the guard still has to be there for the archive
    somebody hand-edited with a different tool."""
    with pytest.raises(SystemExit):
        bstack.backup._member_target("data/config/a\x00b.cfg")


def test_verify_refuses_a_config_member_that_is_far_too_big(bstack, tools):
    """Verification reads the configs into memory; the cap is what stops a
    'printer.cfg' of 8 GB being a memory-exhaustion attack delivered as a
    backup."""
    big = b"\0" * (bstack.backup.MAX_VALIDATE_BYTES + 1)
    path = put(bstack, tools, extra=[("data/config/printer.cfg", "reg", big)])
    with pytest.raises(SystemExit):
        bstack.backup.verify(path.name)


# ------------------------------------------------------- layer 3: the configs -

@pytest.mark.parametrize("printer,moonraker,expect", [
    (GOOD_PRINTER, GOOD_MOONRAKER, True),
    ("max_velocity: 300\n", GOOD_MOONRAKER, False),        # no [section] header
    ("[mcu]\nserial: /dev/a\n", GOOD_MOONRAKER, False),    # no [printer]
    ("[printer]\nmax_velocity: 300\n", GOOD_MOONRAKER, True),
    (GOOD_PRINTER, "not a config at all\n", False),
    (GOOD_PRINTER, "[server]\nhost: 0.0.0.0\n", True),
])
def test_verify_parses_the_configs_it_carries(bstack, tools, printer, moonraker,
                                              expect):
    path = put(bstack, tools, extra=[("data/config/printer.cfg", "reg", printer),
                                     ("data/config/moonraker.conf", "reg",
                                      moonraker)])
    ok, lines = bstack.backup.verify(path.name)
    assert ok is expect, lines


def test_verify_fails_when_printer_cfg_is_absent(bstack, tools):
    members = [m for m in valid_members() if m[0] != "data/config/printer.cfg"]
    path = tools.write_backup(bstack.paths.DATA,
                              "klipperformac-backup-20260101-000000.tar.gz",
                              tools.build(members))
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False
    assert any("printer.cfg is missing" in line for _good, line in lines)


def test_verify_fails_when_moonraker_conf_is_absent(bstack, tools):
    members = [m for m in valid_members()
               if m[0] != "data/config/moonraker.conf"]
    path = tools.write_backup(bstack.paths.DATA,
                              "klipperformac-backup-20260101-000000.tar.gz",
                              tools.build(members))
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False
    assert any("moonraker.conf is missing" in line for _good, line in lines)


def test_verify_accepts_a_cp1252_printer_cfg(bstack, tools):
    """Vendor configs carry German comments saved as cp1252, and Moonraker does
    not require UTF-8. paths.read_config() exists so one non-UTF-8 byte in a
    comment never stops `up`; the same tolerance has to apply here or a perfectly
    good backup would be called broken."""
    text = "[printer]\nmax_velocity: 300  # Größe\n"
    path = put(bstack, tools, extra=[("data/config/printer.cfg", "reg",
                                      text.encode("cp1252"))])
    ok, lines = bstack.backup.verify(path.name)
    assert ok is True, lines


def test_verify_accepts_a_utf8_bom_config(bstack, tools):
    path = put(bstack, tools, extra=[
        ("data/config/printer.cfg", "reg",
         b"\xef\xbb\xbf" + GOOD_PRINTER.encode("utf-8"))])
    ok, _lines = bstack.backup.verify(path.name)
    assert ok is True


def test_verify_warns_about_a_moonraker_conf_without_server_host(bstack, tools):
    """No [server] host means upstream Moonraker binds 0.0.0.0 - the one setting
    `klipperformac lan` exists to control. It restores, but it must not be
    silently wrong about the network."""
    path = put(bstack, tools, extra=[("data/config/moonraker.conf", "reg",
                                      "[data_store]\n")])
    ok, lines = bstack.backup.verify(path.name)
    assert ok is True  # restorable, and flagged
    assert any("binds 0.0.0.0" in line for _good, line in lines)


def test_verify_catches_a_broken_settings_json(bstack, tools):
    path = put(bstack, tools, extra=[("app/settings.json", "reg",
                                      '{"lan": false,}')])
    ok, lines = bstack.backup.verify(path.name)
    assert ok is False
    assert any("not valid JSON" in line for _good, line in lines)


def test_verify_reports_a_missing_manifest_but_keeps_checking(bstack, tools):
    """An archive made by hand (or by `tar czf` on another Mac) has no manifest;
    the files it does carry are still the ones that get validated."""
    path = put(bstack, tools)
    ok, lines = bstack.backup.verify(path.name)
    assert ok is True
    assert any("not written by klipperformac" in line for _good, line in lines)
    assert tools.manifest_of(path) is None


def test_verify_exits_1_from_the_cli(bstack, tools):
    path = put(bstack, tools, extra=[("data/config/printer.cfg", "reg",
                                      "junk without a section\n")])
    with pytest.raises(SystemExit) as exc:
        bstack.cli.main(["backup", "verify", "--from", path.name])
    assert exc.value.code == 1


def test_verify_exits_0_from_the_cli(bstack, tools):
    bstack.backup.create()
    bstack.cli.main(["backup", "verify", "--from", "latest"])


def test_verify_refuses_an_unknown_name(bstack, tools):
    bstack.backup.create()
    with pytest.raises(SystemExit):
        bstack.backup.verify("klipperformac-backup-19990101-000000.tar.gz")


def test_verify_refuses_a_traversing_name(bstack, tools):
    for bad in ("../evil.tar.gz", "config/../x.tar.gz", "/tmp/x.tar.gz",
                ".hidden.tar.gz", "sub/dir.tar.gz"):
        with pytest.raises(SystemExit):
            bstack.backup.verify(bad)


def test_verify_refuses_a_symlink_in_the_backup_folder(bstack, tools):
    """A symlink is not a backup: following it would verify - and let a restore
    read - a file outside DATA/backups."""
    outside = bstack.data.parent / "outside.tar.gz"
    outside.write_bytes(tools.build(valid_members()))
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True, exist_ok=True)
    (where / "klipperformac-backup-20260101-000000.tar.gz").symlink_to(outside)
    with pytest.raises(SystemExit):
        bstack.backup.verify("klipperformac-backup-20260101-000000.tar.gz")


def test_verify_can_read_a_backup_from_an_explicit_path(bstack, tools):
    """Restoring a machine from an archive on another volume is a real case, and
    verify has to be able to look at it without the file being moved in."""
    external = bstack.data.parent / "from-another-mac.tar.gz"
    external.write_bytes(tools.build(valid_members(),
                                     manifest={"klipperformac_backup": 1}))
    ok, _lines = bstack.backup.verify(str(external))
    assert ok is True


def test_verify_never_writes_anywhere(bstack, tools):
    """`verify` is the read-only command: no staging tree, no .tmp, no touched
    config. It must be safe to run against a live stack."""
    path = bstack.backup.create()
    before = sorted(str(p) for p in bstack.paths.DATA.rglob("*"))
    bstack.backup.verify(path.name)
    assert sorted(str(p) for p in bstack.paths.DATA.rglob("*")) == before


def test_manifest_is_strict_json(bstack, tools):
    """The manifest goes through json.dumps(allow_nan=False) like
    paths.save_settings(): a NaN size would make it unreadable by the SwiftUI
    side of this house, and an unsorted one makes two backups differ for no
    reason."""
    path = bstack.backup.create()
    manifest = tools.manifest_of(path)
    assert isinstance(manifest["file_count"], int)
    assert isinstance(manifest["total_bytes"], int)
    raw = json.dumps(manifest, sort_keys=True, allow_nan=False)
    assert "NaN" not in raw and "Infinity" not in raw

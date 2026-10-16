"""Fixtures for the `serial --json` wire contract, pinned across the three waves.

WHO THIS DIRECTORY SERVES
-------------------------
Agent B produces the payload (`klipperformac serial --json`), agent C consumes it
(the SwiftUI Dashboard, `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`), and
agent A owns the reader underneath it (`klipperformac/mcus.py`). Each wave pinned
its own side in its own suite. This suite pins the SEAMS, which nobody owns by
job description:

  * the exact BYTES the producer emits for a frozen input (`serial_json.golden`),
    asserted against the frozen bytes C keeps in its own source as
    `SerialReportDecoder.contractFixture`;
  * the conflict vocabulary, asserted equal across all three spellings of it
    (mcus.CONFLICT_CODES, cli._MCU_CONFLICTS, Swift's `McuConflict` cases);
  * what C's decoder reads, asserted against the keys the producer really emits;
  * the CLI's degrade path (its own `_parse_mcu_sections`, for a machine where
    mcus.py is missing or broken) asserted to answer the same contract.

EVERYTHING HERE IS OFFLINE AND READ-ONLY
----------------------------------------
No /dev node is opened, no `ioreg` runs, no Moonraker call, no stack restart, and
the bench SKR Pico is never reset: the live half of the contract is answered from
`canon_scan.json`, and printer.cfg is written into a throwaway KLIPPERFORMAC_HOME
by the root `stack` fixture. Nothing here may ever read or write the real
~/.klipperformac.

REFRESHING THE GOLDEN (only when the contract changed on purpose)
-----------------------------------------------------------------
    KLIPPERFORMAC_FREEZE_GOLDEN=1 PYTHONPATH=. \
        .venv-test/bin/python -m pytest tests/contract/test_json_bytes_contract.py
Writes `serial_json.golden` from the producer and passes; every other run asserts
against it. The golden is a checked artifact: review its diff like code, and
mirror the new bytes into the Swift fixture (the mirror test tells you to).
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
SWIFT_CONTRACT = REPO / "KlipperMac" / "Sources" / "KlipperMac" / "Core" / "McuSerial.swift"

CANON_CFG = (HERE / "canon_multi_mcu.cfg").read_text()
CANON_FACTS = json.loads((HERE / "canon_scan.json").read_text())
GOLDEN_PATH = HERE / "serial_json.golden"

# The golden file holds the producer's stdout WITHOUT the trailing newline
# `print()` writes, so it stays diffable against a pasted Swift literal.

MASTER = "/dev/cu.usbmodem14101"
TOOL_NODE = "/dev/cu.usbserial-1420"

# The contract, restated here rather than imported, so a rename on one side of
# the seam breaks a test instead of moving the goalposts with it.
CONTRACT_KEYS = ("mcu", "section", "serial", "configured", "claimed",
                 "identity", "conflict")
IDENTITY_KEYS = ("vid_pid", "serial", "key", "present")
#: IN PRECEDENCE ORDER (cause first): the GUI badge picks one of these.
CONFLICT_ORDER = ("bad-section-name", "duplicate-port", "missing-port")


# --------------------------------------------------------------- the producer --
@pytest.fixture
def contract_cli(stack):
    """The CLI + mcus modules bound to this test's throwaway home."""
    import importlib

    mcus = importlib.reload(importlib.import_module("klipperformac.mcus"))
    cli = importlib.reload(importlib.import_module("klipperformac.cli"))
    stack.mcus = mcus
    stack.cli = cli
    return stack


@pytest.fixture
def offline_machine(contract_cli, monkeypatch):
    """The live half of the contract, answered from canon_scan.json.

    detect.scan()/detect.all_identities() are the only two calls this command
    makes to the machine; both get the frozen facts, so the golden bytes cannot
    depend on what is plugged in while the suite runs.
    """
    paths = list(CANON_FACTS["attached"])
    idents = CANON_FACTS["identities"]
    contract_cli.detect.scan = lambda: [
        {"path": path, "description": contract_cli.detect._describe(path)}
        for path in paths]
    contract_cli.detect.all_identities = lambda *a, **k: [
        dict(row) for row in idents]
    return contract_cli


@pytest.fixture
def canon_cfg(offline_machine):
    """The canonical config installed as the printer.cfg the CLI reads."""
    offline_machine.paths.PRINTER_CFG.write_text(CANON_CFG)
    return offline_machine


def _serial_stdout(offline, as_json=True, mcu=None, use_mcus=True):
    """Exactly what `klipperformac serial --json` writes to stdout.

    The real handler, the real argparse-shaped Namespace, and the real bytes:
    no json.loads anywhere, because this suite exists to pin the formatting too.
    """
    cli = offline.cli
    captured, errors = io.StringIO(), io.StringIO()
    real = cli._mcus_module
    if not use_mcus:
        cli._mcus_module = lambda: None
    try:
        with contextlib.redirect_stdout(captured), \
                contextlib.redirect_stderr(errors):
            cli.cmd_serial(argparse.Namespace(set=None, auto=False,
                                               json=as_json, mcu=mcu))
    finally:
        cli._mcus_module = real
    return captured.getvalue(), errors.getvalue()


@pytest.fixture
def serial_json_bytes(canon_cfg):
    """(stdout, stderr) of the read-only contract call on the canonical config."""
    return _serial_stdout(canon_cfg)


@pytest.fixture
def run_serial_json(canon_cfg):
    """Call the producer for any variant this suite needs.

    `run_serial_json(use_mcus=False)` is the degrade path (mcus.py absent);
    `run_serial_json(text=...)` swaps the printer.cfg text first.
    """
    def _run(text=None, use_mcus=True, as_json=True, mcu=None):
        if text is not None:
            canon_cfg.paths.PRINTER_CFG.write_text(text)
        return _serial_stdout(canon_cfg, as_json=as_json, mcu=mcu,
                              use_mcus=use_mcus)
    return _run


@pytest.fixture
def run_serial_set(offline_machine, tmp_path):
    """`serial --set NODE [MCUNAME]` - the WRITE command, captured.

    Returns {exit, out, err}: `exit` is the SystemExit code, None when the
    command returned normally. `use_mcus=False` runs the same write through the
    degrade path (mcus.py missing), which is the other writer this seam has to
    agree with. Only ever pointed at this fixture's throwaway home and a node
    file created under tmp_path, so no write in this suite can reach the real
    ~/.klipperformac or the bench board.
    """
    cli = offline_machine.cli

    def _run(target, mcu=None, as_json=False, text=None, use_mcus=True):
        if text is not None:
            offline_machine.paths.PRINTER_CFG.write_text(text)
        captured, errors = io.StringIO(), io.StringIO()
        code = None
        real = cli._mcus_module
        if not use_mcus:
            cli._mcus_module = lambda: None
        try:
            with contextlib.redirect_stdout(captured), \
                    contextlib.redirect_stderr(errors):
                cli.cmd_serial(argparse.Namespace(set=target, auto=False,
                                                  json=as_json, mcu=mcu))
        except SystemExit as exc:
            code = exc.code
        finally:
            cli._mcus_module = real
        return {"exit": code, "out": captured.getvalue(),
                "err": errors.getvalue()}
    return _run


@pytest.fixture
def device_node(tmp_path):
    """A path that `os.path.exists()` accepts, for the write command's gate.

    A test cannot invent a /dev/cu.* node and must not use the bench's real one,
    so the node is an empty file in this test's own tmp dir.
    """
    def _node(name="fakenode"):
        path = tmp_path / "nodes" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return str(path)
    return _node


@pytest.fixture
def printer_cfg_text(offline_machine):
    """Read the throwaway printer.cfg the command under test writes to.

    The assertions that matter on the write path are 'what did the file become',
    not what the handler returned, so every refusal test needs this.
    """
    return lambda: offline_machine.paths.PRINTER_CFG.read_text()


# ----------------------------------------------------------------- the mirror --
@pytest.fixture(scope="session")
def swift_contract_source():
    """The Swift file that decodes the contract (C's side of the seam)."""
    if not SWIFT_CONTRACT.is_file():
        pytest.fail("the SwiftUI contract decoder moved: update tests/contract "
                    "(looked for %s)" % SWIFT_CONTRACT)
    return SWIFT_CONTRACT.read_text()


def swift_literal_block(golden, width=80, indent=4):
    """The golden bytes, wrapped as a Swift multiline literal ready to paste.

    The chunking is not cosmetic: Swift strips exactly `indent` leading
    characters from every interior line, so a chunk that BEGAN with a space
    would keep it and inject a space into the bytes. Cutting before a comma
    (never after it) keeps every chunk starting with a non-space character, and
    the round trip through `swift_multiline_literal()` is what proves it.
    """
    pad = " " * indent
    chunks, start = [], 0
    while start < len(golden):
        stop = min(start + width, len(golden))
        if stop < len(golden):
            comma = golden.rfind(",", start + 1, stop)
            if comma > start + 1:
                stop = comma
        chunks.append(golden[start:stop])
        start = stop
    assert "".join(chunks) == golden, "wrapping must be lossless"
    lines = []
    for index, chunk in enumerate(chunks):
        lines.append(pad + chunk + ("" if index == len(chunks) - 1 else "\\"))
    return (pad + 'static let contractFixture = """\n' + "\n".join(lines) +
            "\n" + pad + '"""')


def swift_multiline_literal(source, name):
    r"""The value of Swift's `let name = <delims> ... <delims>`, as real bytes.

    Two Swift rules decide this, and both are applied here so the extracted text
    is what the COMPILED app holds rather than what the file's author hoped:
    every interior line loses as much leading whitespace as the CLOSING delimiter
    is indented, and a line ending in a literal backslash contributes no newline.
    The newline before the closing delimiter is not part of the value either.
    """
    open_re = re.compile(r'(?m)^(\s*)(?:static\s+)?let\s+%s\s*=\s*"""' % name)
    started = open_re.search(source)
    assert started, "Swift has no `let %s` multiline literal any more" % name
    lines = source[started.end():].split("\n")
    closing = next((index for index, line in enumerate(lines)
                    if line.strip() == '"""'), None)
    assert closing is not None, "unterminated `\"\"\"` for %s" % name
    indent = len(lines[closing]) - len(lines[closing].lstrip())
    out = []
    for line in lines[:closing]:
        text = line[indent:] if line[:indent].strip() == "" else line
        out.append(text[:-1] if text.endswith("\\") else text)
    return "".join(out)


def swift_conflict_raw_values(source):
    """The raw values of `enum McuConflict: String`, in source order."""
    body = re.search(r"enum McuConflict: String \{(.*?)\n\}", source, re.S)
    assert body, "Swift no longer spells its conflict enum that way"
    return re.findall(r'case\s+\w+\s*=\s*"([^"]+)"', body.group(1))


def swift_decoded_keys(source):
    """Every JSON key the Swift decoder reads, grouped by where it reads it.

    `entry["serial"]` / `dict["vid_pid"]` are how C asks for a field, so this is
    the list of names B may not rename quietly. The identity block is read from
    the `extension McuIdentity` initialiser alone, because the same `dict[...]`
    spelling is used for the top-level envelope in decode().
    """
    entry = set(re.findall(r'entry\["([^"]+)"\]', source))
    head, sep, tail = source.partition("extension McuIdentity")
    root = set(re.findall(r'dict\["([^"]+)"\]', head))
    identity = set(re.findall(r'dict\["([^"]+)"\]', tail)) if sep else set()
    return {"entry": entry, "root": root, "identity": identity}


#: Set (to "1") only when the contract changed on purpose and the golden file
#: is being re-recorded; see this module's docstring for the command.
FREEZE = os.environ.get("KLIPPERFORMAC_FREEZE_GOLDEN") == "1"


@pytest.fixture(scope="session")
def freeze_golden():
    """True when the run was explicitly asked to re-freeze the golden file."""
    return FREEZE


@pytest.fixture
def repo_root():
    return REPO


def pytest_configure(config):
    # sys.path insertion lets the two test modules import this conftest's
    # helpers by file name without tests/ being a package.
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

"""detect.find_by_identity(): the macOS answer to /dev/serial/by-id.

This is the half of the primitive that survives a cable rename -- the reason the
whole layer exists. Multi-MCU bindings (wave 3) persist `usb:1d50:614e:<SER>`
and re-resolve it on every start; `klipperformac flash detect` (S2) does the
same for the board it is about to write. Offline throughout: ioreg is faked.
"""
import pytest

from klipperformac import detect


# --------------------------------------------------------------- rename proof --
def test_find_survives_a_port_rename(fake_run, iokit):
    """The board was /dev/cu.usbmodem14101, macOS handed it 14102 on the next
    plug. Nothing in printer.cfg or in a stored binding has to change."""
    fake_run(text=iokit.one_board())
    ident = detect.identity(iokit.PICO_PATH)
    assert ident["path"] == iokit.PICO_PATH

    fake_run(text=iokit.one_board(path="/dev/cu.usbmodem14102"))
    assert detect.identity(iokit.PICO_PATH) is None            # old name is gone
    assert detect.find_by_identity(ident["vid_pid"], ident["serial"]) == \
        "/dev/cu.usbmodem14102"


def test_find_survives_a_driver_rename(fake_run, iokit):
    """Same board, a different driver family naming it (usbmodem -> usbserial):
    the lookup keys on USB identity, never on the path text."""
    fake_run(text=iokit.one_board(path="/dev/cu.usbserial-A50285BI"))
    assert detect.find_by_identity("1d50:614e", iokit.PICO_SERIAL) == \
        "/dev/cu.usbserial-A50285BI"


def test_round_trip_identity_to_path(fake_run, iokit):
    """identity() output is exactly what find_by_identity() consumes."""
    fake_run(text=iokit.one_board())
    for port in detect.all_identities():
        assert detect.find_by_identity(port["vid_pid"], port["serial"]) == \
            port["path"]
        assert detect.find_by_identity(port["vid_pid"]) == port["path"]


# ------------------------------------------------------- vid_pid input shapes --
@pytest.mark.parametrize("vid_pid", ["1d50:614e", "1D50:614E", " 1d50:614e ",
                                     "0x1d50:0x614e", "1D50:614e"])
def test_vid_pid_spellings_all_match(fake_run, iokit, vid_pid):
    fake_run(text=iokit.one_board())
    assert detect.find_by_identity(vid_pid, iokit.PICO_SERIAL) == iokit.PICO_PATH


@pytest.mark.parametrize("vid_pid", [None, "", "1d50", "1d50:", ":614e",
                                     "1d50:614e:0", "gggh:614e", "1d50:614e5",
                                     "7504:24910", 7504, {}, []])
def test_malformed_vid_pid_matches_nothing(fake_run, iokit, vid_pid):
    """Never raises, never guesses. Note '7504:24910' -- ioreg's DECIMAL for
    this very board -- deliberately does NOT match: vid:pid is HEX by
    convention here (lsusb, system_profiler, identity()'s own output), and
    silently accepting both bases is how the wrong board gets flashed. Take the
    numbers from identity(), or write them as '1d50:614e'."""
    fake_run(text=iokit.one_board())
    assert detect.find_by_identity(vid_pid) is None
    assert detect.find_all_by_identity(vid_pid) == []


@pytest.mark.parametrize("serial", ["5044506110A45B1C", "5044506110a45b1c",
                                    "  5044506110A45B1C  ",
                                    "\t5044506110A45B1C\n"])
def test_serial_compare_is_case_and_surrounding_whitespace_insensitive(
        fake_run, iokit, serial):
    """A serial pasted out of a table or a log line still finds the board;
    INNER whitespace is significant (it is part of the string the firmware
    publishes), so '5044 506110A45B1C' does NOT match."""
    fake_run(text=iokit.one_board())
    assert detect.find_by_identity("1d50:614e", serial) == iokit.PICO_PATH
    assert detect.find_by_identity("1d50:614e", "5044 506110A45B1C") is None


def test_serial_mismatch_is_no_match(fake_run, iokit):
    fake_run(text=iokit.one_board())
    assert detect.find_by_identity("1d50:614e", "NOPE") is None


# ------------------------------------------------------- two identical boards --
def test_two_boards_same_vidpid_different_serial(fake_run, iokit):
    """The multi-MCU bench: two SKR Pics sharing 1D50:614E. The serial is what
    tells the toolhead board from the main board, so a lookup WITH a serial must
    be exact and a lookup WITHOUT one must refuse to pick."""
    main = iokit.usb_board(path="/dev/cu.usbmodem14101", serial="AAAA1111")
    tool = iokit.usb_board(path="/dev/cu.usbmodem14102", serial="BBBB2222",
                           location_id=336658432)
    fake_run(text=iokit.tree_of(main, tool))

    both = detect.all_identities()
    assert [p["path"] for p in both] == ["/dev/cu.usbmodem14101",
                                        "/dev/cu.usbmodem14102"]
    assert {p["serial"] for p in both} == {"AAAA1111", "BBBB2222"}
    assert {p["vid_pid"] for p in both} == {"1D50:614E"}
    assert {p["key"] for p in both} == {"usb:1d50:614e:AAAA1111",
                                       "usb:1d50:614e:BBBB2222"}

    assert detect.find_by_identity("1d50:614e", "AAAA1111") == \
        "/dev/cu.usbmodem14101"
    assert detect.find_by_identity("1d50:614e", "BBBB2222") == \
        "/dev/cu.usbmodem14102"

    # Ambiguous: no serial given, two different boards -> refuse to guess.
    assert detect.find_by_identity("1d50:614e") is None
    assert detect.find_all_by_identity("1d50:614e") == ["/dev/cu.usbmodem14101",
                                                       "/dev/cu.usbmodem14102"]
    assert detect.find_all_by_identity("1d50:614e", "bbbb2222") == \
        ["/dev/cu.usbmodem14102"]


def test_ambiguous_boards_without_any_serial(fake_run, iokit):
    """A fleet of CH340 dongles: same vid:pid, no serials at all. Returning
    paths[0] here would silently reconfigure the wrong printer, so the answer is
    None plus a pick-list."""
    fake_run(text=iokit.tree_of(
        iokit.usb_board(path="/dev/cu.USR2", vid=6790, pid=29987,
                        with_serial=False, location_id=50331648,
                        product="usbserial"),
        iokit.usb_board(path="/dev/cu.USR3", vid=6790, pid=29987,
                        with_serial=False, location_id=67108864,
                        product="usbserial")))
    assert detect.find_by_identity("1A86:7523") is None
    assert detect.find_all_by_identity("1A86:7523") == ["/dev/cu.USR2",
                                                        "/dev/cu.USR3"]


def test_one_board_two_nodes_is_not_ambiguous(fake_run, iokit):
    """A CDC device can publish two nodes for the SAME board (control + data):
    same serial, same locationID. That is one identity group, answered
    deterministically with the first path in sorted order."""
    board = iokit.usb_board()
    board["kids"].append(iokit.serial_client("/dev/cu.usbmodem14100"))
    fake_run(text=iokit.render(iokit.root_tree([board], bluetooth=False)))
    assert detect.find_by_identity("1d50:614e") == "/dev/cu.usbmodem14100"
    assert detect.find_by_identity("1d50:614e", iokit.PICO_SERIAL) == \
        "/dev/cu.usbmodem14100"
    assert detect.find_all_by_identity("1d50:614e") == ["/dev/cu.usbmodem14100",
                                                        "/dev/cu.usbmodem14101"]


def test_single_board_matches_without_serial(fake_run, iokit):
    """The common case: one board attached, caller only knows the vid:pid."""
    fake_run(text=iokit.one_board())
    assert detect.find_by_identity("1d50:614e") == iokit.PICO_PATH


def test_find_when_board_absent(fake_run, iokit):
    """Cable unplugged: None, not a stale path from the last call."""
    fake_run(text=iokit.one_board())
    assert detect.find_by_identity("1d50:614e") == iokit.PICO_PATH
    fake_run(text=iokit.one_board(path="/dev/cu.usbmodem14102",
                                  serial="OTHERSERIAL01"))
    assert detect.find_by_identity("1d50:614e", iokit.PICO_SERIAL) is None
    assert detect.find_all_by_identity("1234:5678") == []


# ------------------------------------------------------------------- listings --
def test_all_identities_sorted_and_complete(fake_run, iokit):
    """The list form `serial --list-json`/the GUI consumes: sorted by path, one
    entry per /dev/cu.* USB serial node, Bluetooth excluded."""
    boards = [iokit.usb_board(path="/dev/cu.usbmodem14103", serial="CCC",
                              location_id=336658433),
              iokit.usb_board(path="/dev/cu.URT1", vid=6790, pid=29987,
                              with_serial=False, product="URT1",
                              location_id=50331648),
              iokit.usb_board(path="/dev/cu.usbmodem14101", serial="AAA"),
              iokit.bluetooth_port()]
    fake_run(text=iokit.tree_of(*boards, bluetooth=False))
    ports = detect.all_identities()
    assert [p["path"] for p in ports] == ["/dev/cu.URT1",
                                          "/dev/cu.usbmodem14101",
                                          "/dev/cu.usbmodem14103"]
    for p in ports:
        assert {"vid_pid", "serial", "name", "ioservice_path"} <= set(p)


def test_all_identities_empty_when_nothing_matches(fake_run, iokit):
    """A Mac with only Bluetooth ports open: [] and no exception, so callers
    print 'no printer found' instead of crashing."""
    fake_run(text=iokit.render(iokit.root_tree([], bluetooth=True)))
    assert detect.all_identities() == []
    assert detect.find_by_identity("1d50:614e") is None


def test_find_functions_accept_timeout_and_stay_bounded(fake_run, iokit):
    rec = fake_run(text=iokit.one_board())
    detect.find_by_identity("1d50:614e", None, timeout=2)
    assert rec.calls[-1]["kwargs"]["timeout"] == 2
    detect.find_all_by_identity("1d50:614e", timeout=99)
    assert rec.calls[-1]["kwargs"]["timeout"] == detect.IOREG_TIMEOUT_CAP

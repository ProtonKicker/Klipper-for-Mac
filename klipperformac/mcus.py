"""The ``[mcu]`` / ``[mcu <name>]`` sections of a printer.cfg: read, validate, rewrite.

WHY THIS MODULE EXISTS
----------------------
Upstream Klipper supports N USB-serial MCUs: ``[mcu]`` is the timing master and
every ``[mcu suffix]`` section is a secondary board clocked by
``clocksync.SecondarySync``. Our surface (``serial``, the GUI picker, the flash
wizard) assumed exactly one board for years, because every reader of printer.cfg
looked only for a literal ``[mcu]``. This module is the single faithful
reader/writer the multi-MCU CLI and the GUI are built on: it reads text and
returns text, so it can be unit-tested against hostile config text and used from
a read-only ``doctor`` pass with no chance of touching a live printer or an
attached board. (Hand it a *path* and it does the byte-safe read/write of that
file for you - see "TEXT OR PATH" below.)

UPSTREAM FACTS THIS CODE IS BUILT ON
------------------------------------
Read these before changing anything here. The semantics below are not our
invention; they are what klippy will do with the file. Paths are relative to
``~/.klipperformac/klipper/``, our pristine (and never edited) checkout.

* ``klippy/mcu.py:1242-1249`` ``add_printer_objects()``::

      mainsync = clocksync.ClockSync(reactor)
      printer.add_object('mcu', MCU(config.getsection('mcu'), mainsync))
      for s in config.get_prefix_sections('mcu '):
          printer.add_object(s.section, MCU(
              s, clocksync.SecondarySync(reactor, mainsync)))

  The master comes from the section named exactly ``mcu`` (lowercase, no
  suffix); secondaries come from ``get_prefix_sections('mcu ')`` -- note the
  TRAILING SPACE in that prefix. The match is case sensitive
  (``klippy/configfile.py:124-126`` is ``s.startswith(prefix)``), so ``[MCU]``
  or ``[Mcu tool]`` builds NO MCU object at all.
* ``klippy/mcu.py:1151-1158`` ``MCU.__init__()``::

      self._name = config.get_name()
      if self._name.startswith('mcu '):
          self._name = self._name[4:]

  Exactly four characters are stripped -- not "mcu", not "whitespace". So
  ``[mcu  a]`` (two spaces) names the MCU ``' a'`` and ``[mcu ]`` names it
  ``''``: plausible-looking text that silently moves an identity.
* ``klippy/mcu.py:1003`` -- that stripped name IS the pin chip:
  ``printer.lookup_object('pins').register_chip(self._name, mcu)``.
* ``klippy/pins.py:126-131`` ``register_chip()`` -- ``chip_name =
  chip_name.strip()``, then ``raise error("Duplicate chip name '%s'")``.
  ``[mcu a]`` and ``[mcu  a]`` both try to own the chip ``a`` and klippy dies at
  connect; a name with an internal space stays ``'to ol'`` and becomes an
  addressable-but-unexpected chip.
* ``klippy/pins.py:71`` ``re_pin = re.compile(r'(?P<prefix>[ _]pin=)(?P<name>[^ ]*)')``
  -- pin translation stops at the first space, so ``pin=to ol:PB3`` cannot work:
  an mcu name must contain no whitespace at all.
* ``klippy/pins.py:78-83`` ``parse_pin()`` -- ``chip_name, pin =
  [s.strip() for s in desc.split(':', 1)]``, default chip ``'mcu'``, unknown chip
  -> ``"Unknown pin chip name '%s'"``. Because that split is at the FIRST colon,
  a ``:`` inside a section suffix can never be addressed back.
* ``klippy/mcu.py:780-791`` ``MCUConnectHelper.__init__()`` -- ``canbus_uuid``
  wins if present; otherwise ``self._serialport = config.get('serial')`` (no
  default, so REQUIRED) and ``config.getint('baud', 250000, minval=2400)``.
* ``klippy/configfile.py:31-37`` -- a missing required option is exactly
  ``"Option 'serial' in section 'mcu tool' must be specified"``.
* ``klippy/configfile.py:160-165`` -- ``data.split('\\n')`` with every line cut
  at its FIRST ``#``, which is why this reader cuts ``#`` anywhere in a line and
  why ``"\\n"`` -- never ``str.splitlines()`` -- is the only real line separator.
* ``klippy/configfile.py:170-176`` and ``:211`` -- parsing is
  ``RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))`` and
  headers are matched with ``RawConfigParser.SECTCRE`` (``r'\\[(?P<header>.+)\\]'``,
  greedy, against the stripped line). Verified against configparser itself and
  reproduced here:
    - the section name is the text between the FIRST ``[`` and the LAST ``]``,
      so ``[mcu tool] ; note`` IS the section ``mcu tool`` while
      ``[mcu] [mcu tool]`` is the junk name ``mcu] [mcu tool``;
    - option names are case-insensitive and both ``:`` and ``=`` separate
      (``Serial = /dev/x`` is a serial line);
    - ``;`` starts a comment only at the start of a line or after whitespace, and
      only inside a VALUE: ``[mcu a;b]`` really is the section ``mcu a;b``,
      ``serial: /dev/a ; x`` reads ``/dev/a`` and ``/dev/a;b`` stays whole;
    - an indented line continues the previous option's value, newlines included,
      which is how ``serial:`` ends up holding a value no device node can match;
    - ``strict=False`` means a repeated option or a repeated section header is
      NOT an error: the section is merged and the LAST value wins silently;
    - ``klippy/configfile.py:430-441`` then rejects a section klippy never loaded
      with ``"Section '...' is not a valid config section"`` -- which is why an
      ignored ``[MCU]`` is not harmless, it stops the printer.
* ``klippy/configfile.py:233-237`` and ``:246-266`` -- everything at or below the
  ``#*# <--- SAVE_CONFIG --->`` marker is generated autosave text, and content
  added after that marker makes klipper log "Can't read autosave from config file
  - modifications after header" and drop EVERY saved probe offset. So writers
  insert above the marker, never at the raw end of file.

TEXT OR PATH
------------
Every function takes either the *text* of a printer.cfg or a *path* to one. Text
in gives text out and touches nothing. A path in is read with
``paths.read_config_text()`` (byte-safe: it never mangles a cp1252 vendor config)
and, for the two writers, written back with ``paths.write_config()`` (atomic
temp-file replace, same encoding as the original). Readers never write, whatever
they are handed.

CONTRACT (shared with the ``serial --json`` CLI layer and the GUI that consume
it; pinned by ``tests/mcus/test_mcus_contract.py``)
-----------------------------------------------------------------------------
``list_mcus()`` never raises about config CONTENT -- malformed sections come back
as entries carrying an issue, never as a traceback, so the CLI can exit 0 with
``conflict`` strings per entry. ``validate()`` turns those entries into issue
records whose ``code`` is one of ``ISSUE_CODES``; the codes a JSON entry may
carry in its ``conflict`` field are ``CONFLICT_CODES`` and ``conflicts_by_mcus()``
is the one mapping the CLI uses, so nobody re-derives it. The writers raise
``McuConfigError`` (a ``ValueError``) instead of silently doing nothing.

Nothing here opens a port, spawns a process or talks to Moonraker.
"""
import configparser
import os
import re

from . import detect, paths

# ------------------------------------------------------------------- constants --
#: The master's section name, exactly as ``config.getsection('mcu')`` wants it
#: (klippy/mcu.py:1246), and the mcu name klippy gives it: ``'mcu'`` does not
#: start with ``'mcu '``, so the four-character strip at mcu.py:1155-1156 leaves
#: it alone.
MASTER_NAME = "mcu"
#: The prefix klippy scans for secondaries -- WITH the trailing space
#: (klippy/mcu.py:1247, ``get_prefix_sections('mcu ')``).
SECONDARY_PREFIX = "mcu "
#: How many characters mcu.py:1156 strips from a section name: exactly ``mcu ``.
NAME_STRIP = 4

#: Advisory ceiling on MCU sections. Upstream has no cap, but a rig with more
#: than this many USB-serial MCUs is a hub-power/bandwidth question and far more
#: often a copy-paste accident that duplicated a section. It is reported as an
#: issue, never enforced by the writers (refusing to write would strand a user
#: who really does have nine boards).
MAX_MCU_SECTIONS = 8

#: klippy's default baud when the option is absent, and its floor:
#: ``config.getint('baud', 250000, minval=2400)`` (klippy/mcu.py:791).
DEFAULT_BAUD = 250000
MIN_BAUD = 2400

#: The keys every consumer of ``list_mcus()`` may rely on. The JSON contract's
#: ``mcus[]`` maps ``name`` -> "mcu" and ``section_header`` -> "section".
MCU_ENTRY_KEYS = ("name", "section_header", "serial", "baud", "line_no")
#: Everything ``list_mcus()`` emits. Additive by design: consumers check
#: ``MCU_ENTRY_KEYS`` so that adding a diagnostic key stays non-breaking.
MCU_ENTRY_ALL_KEYS = MCU_ENTRY_KEYS + (
    "raw_name", "serial_line_no", "serial_count", "serial_raw",
    "baud_configured", "canbus_uuid", "is_master", "is_mcu", "bad_name",
    "header_count", "malformed_header", "header_line",
)

#: Conflicts a JSON entry's ``conflict`` field may carry (null when none).
CONFLICT_CODES = ("bad-section-name", "duplicate-port", "missing-port")
#: Everything ``validate()`` can report: the three conflicts plus findings that
#: are real but are not one section's own identity problem.
ISSUE_CODES = CONFLICT_CODES + (
    "too-many-mcus", "missing-master", "duplicate-section",
    "duplicate-serial-option", "duplicate-chip-name", "bad-serial-value",
    "bad-baud",
)
#: The keys every issue record carries.
ISSUE_KEYS = ("code", "mcu", "section", "line_no", "message")

#: Characters that can never appear in an mcu suffix. ``:`` is klippy's chip
#: separator (klippy/pins.py:78-83 splits at the FIRST colon), ``#`` ends a line
#: during parsing (klippy/configfile.py:161-164), ``;`` ends a value
#: (configfile.py:172) and ``[``/``]`` are the header delimiters themselves.
BAD_NAME_CHARS = (":", "#", ";", "[", "]")

# An option line: the key, then klipper's first ':' or '=' separator. The key is
# lowercased by RawConfigParser's optionxform, which the scanner does too.
_OPTION_RE = re.compile(r"(?P<key>[^\s:=]+)[ \t]*[:=](?P<rest>.*)")
# A ';' only starts an inline comment at the beginning of a line or after
# whitespace (configparser's own comment-prefix match), which is why the value
# '/dev/a;b' survives while '/dev/a ; x' reads '/dev/a'.
_INLINE_RE = re.compile(r"(?:^|\s);")
# klippy/configfile.py:211 recognises section headers with configparser's own
# pattern, so this module matches with that SAME object rather than a
# look-alike: it is what keeps our reader from ever disagreeing with klipper
# about where a section starts. SECTCRE is a documented class attribute of
# ConfigParser; the fallback is its literal pattern (a greedy `.+)`, which is
# why the section name runs to the LAST ']' on the line).
_SECTCRE = getattr(configparser.RawConfigParser, "SECTCRE", None) \
    or re.compile(r"\[(?P<header>.+)\]")


class McuConfigError(ValueError):
    """A writer could not do exactly what was asked, so it changed nothing.

    Subclasses ``ValueError`` on purpose: a caller who only wants "the user asked
    for something impossible" can catch either name. The readers never raise
    about config content.
    """


# ----------------------------------------------------------------- text or path --
def _load(cfg):
    """``(text, encoding)`` -- encoding is None for text input.

    Text input is used as it stands. Anything path-like (``str`` is text, so a
    bare path string is NOT sniffed: pass a ``Path``/``os.PathLike``, which is
    what every caller in this codebase already holds) is read the byte-safe way
    through ``paths.read_config``, which never raises UnicodeDecodeError.
    ``OSError`` from an unreadable file propagates: that is a file problem, not a
    config-content problem, and every CLI caller already handles it.
    """
    if isinstance(cfg, str):
        return cfg, None
    if isinstance(cfg, os.PathLike):
        return paths.read_config(cfg)
    raise TypeError("expected printer.cfg text or a path to one, not %s"
                    % type(cfg).__name__)


def _read(cfg):
    """The text of ``cfg`` (text or path), for readers."""
    return _load(cfg)[0]


def _save(cfg, encoding, text):
    """Write ``text`` back the way it came in: atomic, same encoding."""
    if encoding is None:
        return text
    paths.write_config(cfg, text, encoding)
    return text


# ---------------------------------------------------------------------- parsing --
class _Section:
    """One parsed section: where it is, and the options klippy would see."""

    __slots__ = ("raw_name", "line_no", "options", "header_count",
                 "malformed_header", "header_line")

    def __init__(self, raw_name, line_no, malformed_header=None,
                 header_line=None):
        self.raw_name = raw_name
        self.line_no = line_no
        # The header line exactly as the file writes it, stripped of leading/
        # trailing whitespace ('[mcu tool] ; note'). `detect.set_serial_text()`
        # matches a section by that verbatim text, so handing it back is what
        # lets a write work on a header klipper accepts with a comment after
        # the ']' - without either module inventing a second header rule.
        self.header_line = header_line
        # {lowercased key: (effective value, first line no, definition count)}.
        # The effective value is the LAST definition's, because klipper parses
        # with strict=False (klippy/configfile.py:172): a repeated option is not
        # an error, it overwrites. `first line no` is the line a writer rewrites.
        self.options = {}
        # Repeating a section header does not create a second section upstream
        # either (configparser merges it into the first), so we keep one record
        # and count the headers: that makes a silent override visible.
        self.header_count = 1
        # The verbatim text of a header klipper could not parse at all ("[mcu
        # tool" with no closing bracket), else None.
        self.malformed_header = malformed_header

    def define(self, key, value, line_no):
        found = self.options.get(key)
        if found is None:
            self.options[key] = (value, line_no, 1)
        else:
            self.options[key] = (value, found[1], found[2] + 1)

    def append_continuation(self, key, text, blanks):
        """Join an indented line into its option's value, as configparser does.

        Blank and comment lines between them become part of the value too
        (``serial: /dev/a`` + blank line + ``  /dev/b`` reads ``/dev/a\\n\\n/dev/b``).
        """
        value, first, count = self.options[key]
        self.options[key] = (value + "\n" * (1 + blanks) + text, first, count)

    def get(self, key):
        return self.options.get(key.lower())

    def value(self, key):
        found = self.options.get(key.lower())
        return None if found is None else found[0]


def cut_line_comment(line):
    """The line as klipper reads it: cut at the first '#', wherever it is.

    klippy/configfile.py:161-164 slices every line at ``line.find('#')`` BEFORE
    parsing, so a '#' in the middle of a value truncates it for klipper too.
    """
    pos = line.find("#")
    return line if pos < 0 else line[:pos]


def section_header(body):
    """``(is_header, section_name_or_None)`` for a '#'-stripped, stripped line.

    The rule klipper applies (klippy/configfile.py:211, ``SECTCRE.match``): the
    line must start with '[' and the name is everything up to the LAST ']', so
    '[mcu tool] ; note' IS the section 'mcu tool' and '[mcu] [mcu x]' is the junk
    name 'mcu] [mcu x'. A line that starts with '[' but never closes is a header
    klipper rejects outright (configparser raises on the whole file): reported
    here as "a header, unparseable name" so callers can surface it instead of
    silently losing the section.
    """
    if not body.startswith("["):
        return False, None
    match = _SECTCRE.match(body)
    if match:
        return True, match.group("header")
    return True, None


def option_value(rest):
    """An option's value: inline ``;`` comment cut, then stripped."""
    match = _INLINE_RE.search(rest)
    if match:
        rest = rest[:match.start()]
    return rest.strip()


def _scan(cfg_text):
    """Split printer.cfg text into the sections klippy would build.

    Splitting is ``text.split("\\n")`` on purpose: klipper does
    ``data.split('\\n')`` (klippy/configfile.py:160) and hands the buffer to
    ``read_file()``, which iterates on ``\\n`` only. ``str.splitlines()`` would
    also break on ``\\v``, ``\\f``, ``\\x85`` and ``\\u2028`` and could invent a
    section klipper never sees.

    Section headers are recognised with `section_header()` above -- the rule
    klipper itself applies at klippy/configfile.py:211. `detect.set_serial_text()`
    matches a header more strictly (the whole stripped line must equal the
    header), so the two can disagree about an exotic line; `set_serial()` re-reads
    its own output and refuses when they do, which makes a disagreement a loud
    no-write rather than a wrong write.
    """
    sections = []
    by_name = {}
    section = None
    open_option = None   # lowercased key an indented line would continue
    blanks = 0           # blank/comment lines since the last content line
    for line_no, raw in enumerate(cfg_text.split("\n"), 1):
        body = cut_line_comment(raw).strip()
        if not body or body[0] == ";":
            blanks += 1
            continue
        is_header, raw_name = section_header(body)
        if is_header:
            written = raw.strip()
            if raw_name is None:
                # "[mcu tool" with no ']': keep what the user meant (the text
                # after the bracket) so the section is still reported, with the
                # unparseable header preserved in malformed_header.
                section = _Section(body[1:], line_no, malformed_header=body,
                                   header_line=written)
                sections.append(section)
                by_name.setdefault(section.raw_name, section)
            else:
                existing = by_name.get(raw_name)
                if existing is not None and existing.malformed_header is None:
                    # A repeated header merges into the first section upstream;
                    # count it so validate() can report the silent override.
                    existing.header_count += 1
                    section = existing
                else:
                    section = _Section(raw_name, line_no, header_line=written)
                    sections.append(section)
                    by_name.setdefault(raw_name, section)
            open_option = None
            blanks = 0
            continue
        if section is None:
            # An option before any section header: klipper refuses the whole
            # file (MissingSectionHeaderError). There is no section to attach it
            # to and no MCU to report, so it is dropped here; the file-level
            # issues (missing-master) still describe the state honestly.
            open_option = None
            continue
        if raw[:1].isspace() and open_option is not None:
            section.append_continuation(open_option, body, blanks)
            blanks = 0
            continue
        match = _OPTION_RE.match(body)
        if match is None or " " in match.group("key"):
            # "serial /dev/x" (no separator) or a key with a bare space: neither
            # is an option klipper reads as serial/baud, so neither is ours to
            # guess at.
            open_option = None
            blanks = 0
            continue
        key = match.group("key").lower()
        section.define(key, option_value(match.group("rest")), line_no)
        open_option = key
        blanks = 0
    return sections


def is_mcu_section(raw_name):
    """True when klippy would build an MCU object for this section name.

    ``mcu`` exactly (the master, klippy/mcu.py:1246) or ``mcu `` + anything
    (klippy/mcu.py:1247 -- that prefix carries a trailing space). Case matters,
    and so does that space: ``[MCU]``, ``[Mcu tool]`` and ``[mcu]`` with a
    trailing space in the file are not mcu sections as far as klipper is
    concerned.
    """
    return raw_name == MASTER_NAME or raw_name.startswith(SECONDARY_PREFIX)


def _mcu_like(raw_name):
    """mcu sections, plus everything a human clearly meant as one.

    ``[MCU]`` and ``[Mcu tool]`` build no MCU (the mcu.py:1242-1249 lookup is
    case sensitive) and then fail validation as unknown sections
    (configfile.py:430-441), so hiding them would hide the very bug this reader
    exists to catch. They come back as entries with ``is_mcu`` false.

    The third branch is the two-brackets-per-line accident: ``[mcu] [mcu tool]``
    on one line. SECTCRE's greedy ``.+)`` makes klipper read the section name as
    ``mcu] [mcu tool`` -- a name that is neither the master nor a secondary, and
    that it rejects at the end of config load. Reporting it as an entry beats
    reporting a config with "no MCU at all".
    """
    lowered = raw_name.lower()
    return (is_mcu_section(raw_name)
            or lowered == MASTER_NAME
            or lowered.startswith(SECONDARY_PREFIX)
            or (MASTER_NAME in lowered and "]" in raw_name))


def name_problem(name, is_master=False):
    """Why klipper would refuse or mangle this mcu name, or None when it is fine.

    These are upstream rules, not taste: mcu.py:1151-1158 strips exactly the four
    characters ``mcu ``, and the result is registered as the pin chip name after
    another ``.strip()`` (klippy/pins.py:126-131) and used verbatim as the
    ``<name>:`` prefix a user must type (klippy/pins.py:78-83). A name that
    ``.strip()`` changes, that pin translation cannot reach, or that collides with
    the master's own chip is a silent identity change, not a cosmetic one.
    """
    if is_master:
        return None
    if not name:
        return ("is empty: '[mcu ]' leaves nothing after the four characters "
                "klipper strips, so the board has no name at all")
    if name != name.strip():
        return ("has a leading or trailing space: klipper registers the pin "
                "chip as '%s' while this header says '%s'"
                % (name.strip(), name))
    if any(char.isspace() for char in name):
        return ("contains a space: pin translation stops at the first space "
                "(klippy/pins.py:71), so 'pin=%s:PB1' cannot work" % (name,))
    bad = [char for char in BAD_NAME_CHARS if char in name]
    if bad:
        return ("contains '%s', which a pin prefix like '%s:PB1' can never "
                "reproduce (klippy/pins.py:78-83)" % (bad[0], name))
    if name == MASTER_NAME:
        return ("is 'mcu', which collides with the timing master's own pin chip "
                "(klippy/pins.py:126-131)")
    return None


def header_for(mcu_name):
    """The section header klipper uses for this mcu name: ``[mcu]``/``[mcu x]``."""
    if mcu_name == MASTER_NAME:
        return "[mcu]"
    return "[" + SECONDARY_PREFIX + mcu_name + "]"


def _entry(section):
    """One scanned section -> the entry dict the contract describes."""
    raw_name = section.raw_name
    master = raw_name == MASTER_NAME
    entry_is_mcu = is_mcu_section(raw_name) and section.malformed_header is None
    if master:
        name = MASTER_NAME
    elif entry_is_mcu:
        # mcu.py:1154-1156: strip EXACTLY "mcu " (4 characters), nothing more.
        name = raw_name[NAME_STRIP:]
    else:
        # Not an mcu section for klipper ('MCU', 'Mcu tool', junk text): report
        # what the file says, so the conflict string has something to point at.
        name = raw_name
    serial_found = section.get("serial")
    baud_found = section.get("baud")
    serial_raw = serial_found[0] if serial_found is not None else None
    baud = None
    if baud_found is not None:
        try:
            baud = int(baud_found[0])
        except (TypeError, ValueError):
            baud = None
    problem = None if master else name_problem(name, False)
    bad_name = (section.malformed_header is not None
                or not entry_is_mcu or problem is not None)
    return {
        "name": name,
        # The header klipper names the object by: brackets around the section
        # name it parsed, e.g. "[mcu tool]". This is the contract's "section".
        "section_header": "[" + raw_name + "]",
        # Empty/whitespace-only and multi-line (continuation-mangled) values read
        # as "no port configured": klipper would get '' or a two-line string and
        # fail to open it, which is the same user-facing problem as the line
        # being absent (mcu.py:788). ``serial_raw`` keeps what was written.
        "serial": serial_raw if serial_raw and "\n" not in serial_raw else None,
        "baud": baud,
        "line_no": section.line_no,
        "raw_name": raw_name,
        # The FIRST serial: line, i.e. the one a write rewrites. When a section
        # repeats the option the value klipper reads comes from the LAST one,
        # which is why validate() reports duplicate-serial-option.
        "serial_line_no": serial_found[1] if serial_found is not None else None,
        "serial_count": serial_found[2] if serial_found is not None else 0,
        "serial_raw": serial_raw,
        "baud_configured": baud_found is not None,
        "canbus_uuid": section.value("canbus_uuid") or None,
        "is_master": master,
        "is_mcu": entry_is_mcu,
        "bad_name": bad_name,
        "header_count": section.header_count,
        "malformed_header": section.malformed_header,
        "header_line": section.header_line,
    }


def list_mcus(cfg):
    """Every MCU section in a printer.cfg, ordered the way klipper loads them.

    ``cfg`` is the text of a printer.cfg or a path to one (read-only either way).
    Returns ``[{name, section_header, serial, baud, line_no, ...}]``: the master
    ``[mcu]`` first (it is the timing master, klippy/mcu.py:1246), then every
    ``[mcu suffix]`` in file order. Keys past ``MCU_ENTRY_KEYS`` are additive; see
    ``MCU_ENTRY_ALL_KEYS``.

    ORDER, so the seam never argues about it: this is klipper's own load order
    (master first, then the sections as the file spells them), which is what a
    writer and a ``validate()`` report need. The ``serial --json`` layer re-sorts
    it for the contract - master first, then by mcu name - so a GUI list is stable
    whatever order a human pasted the sections in. Both orders put the master at
    index 0; neither promises the rest to be file order, and
    ``tests/contract/test_json_bytes_contract.py`` pins the JSON one.

    ``serial`` and ``baud`` are the values klipper would actually read: the LAST
    definition wins when a section repeats an option (``strict=False``,
    configfile.py:172), a value stops at its first ``#`` or at a
    whitespace-preceded ``;``, and an indented continuation line is joined into it
    (which is why such a serial comes back as ``None`` -- no device node can match
    a two-line value; ``serial_raw`` shows what the file says). ``line_no`` is the
    section header's line, ``serial_line_no`` the line a write would change, and
    ``baud`` is ``None`` for both "absent" (klipper's default is 250000,
    mcu.py:791) and "not an integer" -- ``baud_configured`` tells those apart.

    NEVER raises about config CONTENT: broken, hostile or empty text yields a list
    (possibly empty), never a traceback -- which is what lets ``serial --json``
    exit 0 carrying per-entry conflicts instead of dying on a user's typo. An
    unreadable PATH still raises OSError, because that is a file problem and every
    caller already handles those.
    """
    entries = [_entry(section) for section in _scan(_read(cfg))
               if _mcu_like(section.raw_name)]
    entries.sort(key=lambda entry: (0 if entry["is_master"] else 1,
                                    entry["line_no"]))
    return entries


# ------------------------------------------------------------------- validating --
def _issue(code, entry, message):
    return {
        "code": code,
        "mcu": None if entry is None else entry["name"],
        "section": None if entry is None else entry["section_header"],
        "line_no": None if entry is None else entry["line_no"],
        "message": message,
    }


def _bad_name_issue(entry):
    """The bad-section-name issue for one entry, or None when it has none."""
    if entry["malformed_header"] is not None:
        return _issue(
            "bad-section-name", entry,
            "%s has no closing ']' (it is written '%s'), which makes klipper "
            "reject the whole config file instead of reading any of it"
            % (entry["section_header"], entry["malformed_header"]))
    if not entry["is_mcu"]:
        return _issue(
            "bad-section-name", entry,
            "%s is not an MCU section for klipper, which loads '[mcu]' and "
            "'[mcu <name>]' only (klippy/mcu.py:1242-1249), so this board is "
            "dead config and klipper stops with \"Section '%s' is not a valid "
            "config section\" (klippy/configfile.py:430-441)"
            % (entry["section_header"], entry["raw_name"].lower()))
    problem = name_problem(entry["name"], entry["is_master"])
    if problem is None:
        return None
    return _issue(
        "bad-section-name", entry,
        "%s %s - klipper strips exactly 'mcu ' from the header to get the board's "
        "pin identity (klippy/mcu.py:1151-1158), so this is not a label"
        % (entry["section_header"], problem))


def validate(mcus):
    """Issues for a config; ``[]`` means klipper would start it as written.

    Takes the ``list_mcus()`` output (the documented input), or config text, or a
    path -- the latter two go through ``list_mcus()`` first, so a caller with a
    file has one call to make.

    Each issue is ``{code, mcu, section, line_no, message}`` with ``code`` from
    ``ISSUE_CODES``. Three codes are the JSON contract's conflicts
    (``CONFLICT_CODES``) and belong on an entry; use ``conflicts_by_mcus()`` for
    that mapping instead of re-deriving it. The rest are whole-file findings that
    belong in prose, not on one row.

    What gets flagged, and why each is a real klipper failure rather than style:
      * ``bad-section-name`` - the name klipper derives is not the name the user
        typed (mcu.py:1156 strips four characters, pins.py:127 then ``.strip()``s
        it), or the section is not an mcu section at all (mcu.py:1247's prefix is
        case sensitive and carries a trailing space), or its header cannot be
        parsed.
      * ``missing-port`` - no ``serial:`` and no ``canbus_uuid:`` to connect
        (mcu.py:780-791), i.e. "Option 'serial' in section ... must be specified"
        (configfile.py:31-37).
      * ``duplicate-port`` - two sections name the same node: klipper takes a
        serial port exclusively, so the second board never connects.
      * ``duplicate-chip-name`` - two names collapse to one pin chip after
        pins.py:127's ``.strip()``: klipper stops at connect with "Duplicate chip
        name".
      * ``duplicate-section`` / ``duplicate-serial-option`` - ``strict=False``
        makes both silent in klipper (the last value wins), which is exactly why
        they have to be said out loud somewhere.
      * ``bad-serial-value`` / ``bad-baud`` - a continuation line or a typo put
        something unusable in a value klipper cannot open or parse.
      * ``missing-master`` / ``too-many-mcus`` - file-level sanity.
    """
    if not isinstance(mcus, list):
        mcus = list_mcus(mcus)
    issues = []
    for entry in mcus:
        bad_name = _bad_name_issue(entry)
        if bad_name is not None:
            issues.append(bad_name)
        if not entry["is_mcu"]:
            continue
        if entry["serial"] is None and entry["canbus_uuid"] is None:
            issues.append(_issue(
                "missing-port", entry,
                "%s has no usable serial: line, so klipper has nothing to "
                "connect; a CAN board would say canbus_uuid: instead "
                "(klippy/mcu.py:780-791)" % (entry["section_header"],)))
        if entry["serial_count"] > 1:
            issues.append(_issue(
                "duplicate-serial-option", entry,
                "%s defines serial: %d times: klipper keeps the LAST one and "
                "ignores the rest (strict=False, klippy/configfile.py:172), while "
                "line %d is the one a write would change - fix it by hand"
                % (entry["section_header"], entry["serial_count"],
                   entry["serial_line_no"])))
        if entry["serial_raw"] is not None and "\n" in entry["serial_raw"]:
            issues.append(_issue(
                "bad-serial-value", entry,
                "%s's serial: value continues onto the next (indented) line, which "
                "klipper joins into one value (klippy/configfile.py:172), so it "
                "names no device node at all" % (entry["section_header"],)))
        if entry["baud_configured"]:
            if entry["baud"] is None:
                issues.append(_issue(
                    "bad-baud", entry,
                    "%s has a baud: that is not an integer, which klipper reports "
                    "as \"Unable to parse option 'baud' in section '%s'\""
                    % (entry["section_header"], entry["raw_name"])))
            elif entry["baud"] < MIN_BAUD:
                issues.append(_issue(
                    "bad-baud", entry,
                    "%s has baud: %d, under the minimum %d klipper accepts "
                    "(config.getint('baud', 250000, minval=2400))"
                    % (entry["section_header"], entry["baud"], MIN_BAUD)))
    issues.extend(_file_issues(mcus))
    return issues


def _file_issues(mcus):
    """Whole-file findings: repeated headers, shared ports, shared chips, count."""
    issues = []
    for entry in mcus:
        if entry["header_count"] > 1:
            issues.append(_issue(
                "duplicate-section", entry,
                "%s appears %d times (first at line %d): klipper merges them and "
                "keeps the last value of every repeated option, so the earlier "
                "copy is silently discarded"
                % (entry["section_header"], entry["header_count"],
                   entry["line_no"])))
    owners = {}
    for entry in mcus:
        if entry["is_mcu"] and entry["serial"]:
            owners.setdefault(entry["serial"], []).append(entry)
    for serial in sorted(owners):
        holders = owners[serial]
        if len(holders) < 2:
            continue
        for entry in holders:
            others = [other["section_header"] for other in holders
                      if other is not entry]
            issues.append(_issue(
                "duplicate-port", entry,
                "%s and %s both claim %s: klipper holds a serial port exclusively, "
                "so one of the two boards cannot connect"
                % (entry["section_header"], " and ".join(others), serial)))
    chips = {}
    for entry in mcus:
        if entry["is_mcu"]:
            # klippy/pins.py:126-131 - register_chip() strips the name, then
            # raises "Duplicate chip name" when two MCUs land on one chip.
            chips.setdefault((entry["name"] or "").strip(), []).append(entry)
    for chip in sorted(chips):
        holders = chips[chip]
        if len(holders) < 2:
            continue
        for entry in holders:
            issues.append(_issue(
                "duplicate-chip-name", entry,
                "%s registers the pin chip '%s', which another section already "
                "owns: klipper stops with \"Duplicate chip name '%s'\" "
                "(klippy/pins.py:126-131)"
                % (entry["section_header"], chip, chip)))
    if not any(entry["is_master"] for entry in mcus):
        issues.append(_issue(
            "missing-master", None,
            "this config has no '[mcu]' section, and klipper always builds the "
            "timing master from it (klippy/mcu.py:1242-1249), so it stops with "
            "\"Option 'serial' in section 'mcu' must be specified\""))
    count = len([entry for entry in mcus if entry["is_mcu"]])
    if count > MAX_MCU_SECTIONS:
        issues.append(_issue(
            "too-many-mcus", None,
            "%d MCU sections is past the %d this surface sanity-checks; klipper "
            "would still try to connect every one of them"
            % (count, MAX_MCU_SECTIONS)))
    return issues


def conflicts_by_mcus(mcus):
    """``{mcu name: conflict string or None}`` - a JSON entry's ``conflict``.

    First conflict wins, in ``CONFLICT_CODES`` order, so a section that is both
    badly named and portless reports the name (the cause) rather than the missing
    port (the symptom). Two sections sharing a header name collapse to one entry
    upstream (configparser merges them), so name keys are safe here.
    """
    issues = validate(mcus)
    out = {entry["name"]: None for entry in (
        mcus if isinstance(mcus, list) else list_mcus(mcus))}
    for code in CONFLICT_CODES:
        for issue in issues:
            if issue["code"] != code or issue["mcu"] is None:
                continue
            if out.get(issue["mcu"]) is None:
                out[issue["mcu"]] = code
    return out


def issues_for(mcus, name):
    """Every issue about one mcu name (``[]`` when that section is clean)."""
    return [issue for issue in validate(mcus) if issue["mcu"] == name]


def _find(mcus, mcu_name):
    """The entry a caller means by ``mcu_name``, or None.

    Accepts the mcu name (``tool``, or ``mcu`` for the master) and the header as
    written (``[mcu tool]``), because ``list_mcus()`` hands back both and a CLI
    will have one of them in hand.
    """
    wanted = mcu_name.strip() if isinstance(mcu_name, str) else ""
    if wanted.startswith("[") and wanted.endswith("]"):
        inner = wanted[1:-1]
        if inner == MASTER_NAME:
            wanted = MASTER_NAME
        elif inner.startswith(SECONDARY_PREFIX):
            wanted = inner[NAME_STRIP:]
    for entry in mcus:
        if entry["name"] == wanted:
            return entry
    return None


# ---------------------------------------------------------------------- writing --
def _missing_target_error(cfg_text, mcu_name):
    """A refusal that names what the file does have; never a bare KeyError."""
    entries = list_mcus(cfg_text)
    found = ", ".join(entry["section_header"] for entry in entries) or \
        "no MCU section at all"
    wanted = str(mcu_name).strip()
    near = [entry for entry in entries if entry["name"].strip() == wanted]
    if near:
        # The board is in the file under a header klipper reads differently than
        # the name it would register: refusing is the only honest answer, and
        # "[mcu a] exists" would send the user looking for a section that does
        # not exist in the way they expect.
        return McuConfigError(
            "There is no %s to write: the file has %s, which klipper strips to "
            "the mcu name %r (four characters, klippy/mcu.py:1151-1158), not %r. "
            "Fix that header by hand - it is the board's pin identity, not a "
            "label."
            % (header_for(mcu_name), near[0]["section_header"],
               near[0]["name"], wanted))
    return McuConfigError("Nothing to write: this config has %s, not %s."
                          % (found, header_for(mcu_name)))


def _crlf_insertion_fixup(text, updated):
    """Give an inserted ``serial:`` line the file's own CRLF.

    ``detect.set_serial_text()`` rewrites an existing line with that line's own
    ending (so a CRLF config stays CRLF) but, exactly as ``write_serial`` has
    always done, ADDS a missing line with a bare LF. One mixed-ending file is not
    worth changing the shared kernel's long-standing behaviour over, so the added
    line is corrected here, where the section context is known: if the line above
    it (the header) or below it ends with ``\\r``, so does the new serial line.
    """
    if "\r\n" not in text:
        return updated
    before = text.split("\n")
    after = updated.split("\n")
    if len(after) != len(before) + 1:
        return updated           # a replace, not an insert: nothing to fix
    for index, old in enumerate(before):
        if old == after[index]:
            continue
        new = after[index]
        above = after[index - 1] if index else ""
        if new.startswith("serial: ") and not new.endswith("\r") \
                and (above.endswith("\r") or old.endswith("\r")):
            after[index] = new + "\r"
        return "\n".join(after)
    return updated


def set_serial(cfg, mcu_name, device):
    """Set ``mcu_name``'s ``serial:`` to ``device``; returns the new text.

    ``cfg`` is printer.cfg text (nothing is touched but the returned string) or a
    path to one (read byte-safe and rewritten atomically through
    ``paths.write_config``, same encoding as the original - that is what
    ``serial --set`` does).

    Every byte that is not the ``serial:`` line stays exactly where it was: other
    sections, comments, blank lines, CRLF endings, indentation. The ``serial:``
    line itself is replaced whole, so an inline comment on THAT line goes with it
    (the rule ``detect.write_serial`` has always had).

    There is one implementation of "set a section's serial", not two: this is
    ``detect.set_serial_text()`` -- the extracted kernel of
    ``detect.write_serial()``, which has always owned the master's serial line --
    called with the target's header, so ``[mcu]`` and ``[mcu tool]`` behave
    identically and ``serial --set`` cannot disagree with itself per section. The
    header handed to the kernel is the section's own line as the file writes it
    (``header_line``), which is how a header klipper accepts with a comment after
    the ``]`` -- ``[mcu tool] ; the toolboard`` -- still gets written.

    The result is then RE-READ with ``list_mcus()`` and must agree: the section is
    still there and its effective serial is exactly ``device``. A config the
    klipper-faithful reader and the line-oriented writer disagree about (a
    repeated ``serial:`` line, a ``serial =`` spelling, a header whose text the
    kernel cannot match) is refused loudly instead of half-written, because an
    exit 0 that did not fix the port is a lie CI, scripts and the GUI all believe.

    Raises ``McuConfigError`` (and changes nothing) when:
      * ``mcu_name`` is not in the file - the message lists what is;
      * the section's header would not survive klipper's own parsing
        (``[mcu  a]``, ``[mcu ]``, ``[MCU]``, a header with no ``]``): writing a
        port into a section klipper ignores, or into a chip identity no one can
        address by name, would report success and stay broken;
      * ``device`` spans lines or contains a NUL, which could inject config lines
        (the guard ``detect.write_serial`` has always had), or is blank, which
        klipper would try to open as a port.
    """
    text, encoding = _load(cfg)
    if not isinstance(device, str) or re.search(r"[\r\n\0]", device):
        raise McuConfigError(
            "A device path must be one line with no newlines: %r" % (device,))
    if not device.strip():
        # `serial:` with nothing after it is what klipper fails on at connect
        # (mcu.py:788 gives it an empty port to open), so it is never a write.
        raise McuConfigError("A device path cannot be blank")
    if not isinstance(mcu_name, str) or not mcu_name.strip():
        raise McuConfigError("An MCU name is required")
    entries = list_mcus(text)
    entry = _find(entries, mcu_name)
    if entry is None:
        raise _missing_target_error(text, mcu_name)
    bad_name = _bad_name_issue(entry)
    if bad_name is not None:
        raise McuConfigError(
            "Refusing to write a port into %s - %s. Fix the section header first: "
            "klipper strips exactly 'mcu ' from it to get the pin chip name, so a "
            "wrong header is a wrong board."
            % (entry["section_header"], bad_name["message"]))
    if entry["serial_count"] > 1:
        raise McuConfigError(
            "%s defines serial: %d times (lines from %d on). Klipper reads the "
            "LAST one (strict=False, klippy/configfile.py:172), so writing this "
            "section would look like success and leave the old port in charge - "
            "delete the extra lines by hand first."
            % (entry["section_header"], entry["serial_count"],
               entry["serial_line_no"]))
    header = entry["header_line"] or header_for(entry["name"])
    updated = detect.set_serial_text(text, device, header)
    if updated is None:
        if header != header_for(entry["name"]):
            raise McuConfigError(
                "%s is written '%s' on its line. Klipper reads that as the "
                "section %s (it drops whatever follows the ']'), but this tool "
                "only rewrites a header line that is exactly '%s' with nothing "
                "after the ']', so nothing was changed: move that comment to a "
                "line of its own and run again."
                % (entry["section_header"], header, entry["section_header"],
                   header_for(entry["name"])))
        raise McuConfigError(
            "Could not find the section %s to write; nothing was changed."
            % (entry["section_header"],))
    updated = _crlf_insertion_fixup(text, updated)
    check = _find(list_mcus(updated), entry["name"])
    if check is None or check["serial"] != device:
        raise McuConfigError(
            "Writing %s did not produce a port klipper would read: the section "
            "was re-read and says %r. Nothing was changed. The usual reasons are "
            "two serial: lines in one section (klipper keeps the last, "
            "klippy/configfile.py:172) or the option spelled 'serial = ...', "
            "which klipper accepts and this line writer does not rewrite - fix it "
            "by hand to 'serial: %s'."
            % (header, check["serial"] if check else None, device))
    return _save(cfg, encoding, updated)


# The stub's reminder is the part every multi-MCU user gets wrong on the first
# try, so it ships with the section instead of in a doc nobody opens.
_STUB_COMMENTS = (
    "# Second MCU: the single word after 'mcu ' in the header above IS how",
    "# this board's pins are addressed from the rest of the config, e.g.",
    "#   step_pin: {name}:PB1   (plain PB1 is the master's pin space).",
    "# Klipper strips exactly the four characters 'mcu ' to get that name",
    "# (klippy/mcu.py:1151-1158) and registers it as the pin chip",
    "# (klippy/mcu.py:1003, klippy/pins.py:126-131), so a stray space, a",
    "# second word or a colon silently changes the chip's identity:",
    "# '[mcu  {name}]' is not '[mcu {name}]', and two sections that",
    "# collapse to one name stop klipper with \"Duplicate chip name\".",
    "# No serial: line yet, deliberately: klipper's own error ('Option",
    "# 'serial' in section 'mcu {name}' must be specified') points right",
    "# at it, and 'klipperformac serial' writes the port in for you.",
)


def add_mcu_section(cfg, name):
    """Add a correct ``[mcu <name>]`` stub; returns the new text.

    ``cfg`` is printer.cfg text (nothing touched) or a path (read byte-safe,
    rewritten atomically, same encoding).

    Text in, text out, byte-preserving for everything already in the file. The
    stub goes after the last line of the editable region -- and when the file has
    a ``SAVE_CONFIG`` block, BEFORE it, because anything below that marker is
    generated autosave text: adding after it makes klipper log "Can't read
    autosave from config file - modifications after header" and stop applying
    every saved probe offset (klippy/configfile.py:233-237 and :246-266).

    Refuses (raises ``McuConfigError``, changes nothing) when the name would not
    survive klipper's own parsing, when it is the master's name, or when a
    section for that mcu is already there: a second ``[mcu tool]`` merges into the
    first one (``strict=False``, configfile.py:172) and the earlier copy's
    settings vanish without a word.

    The stub carries no ``serial:`` and no ``baud:`` line on purpose: baud has an
    upstream default (250000, klippy/mcu.py:791) and a missing serial produces
    klipper's most actionable error. ``set_serial()`` adds the line where it
    belongs.
    """
    text, encoding = _load(cfg)
    if not isinstance(name, str) or not name or name != name.strip():
        raise McuConfigError(
            "An MCU name is required, with no leading or trailing space: %r"
            % (name,))
    problem = name_problem(name)
    if problem is not None:
        raise McuConfigError("Cannot add %s: %s" % (header_for(name), problem))
    for entry in list_mcus(text):
        if entry["name"] == name or entry["raw_name"] == SECONDARY_PREFIX + name:
            raise McuConfigError(
                "%s already exists (line %d); Klipper merges duplicate sections "
                "and keeps the last copy of every repeated option, so adding a "
                "second one would quietly discard the first"
                % (entry["section_header"], entry["line_no"]))
    eol = _line_ending(text)
    stub = eol.join([header_for(name)]
                    + [line.format(name=name) for line in _STUB_COMMENTS]) + eol
    return _save(cfg, encoding, _insert_section(text, stub, eol))


def _line_ending(cfg_text):
    """The file's own line ending, so an added section never mixes styles.

    CRLF wins if the text has any CRLF at all: a printer.cfg that came out of
    Windows-side tooling is CRLF top to bottom, and half-CRLF config files are a
    support burden nobody needs.
    """
    return "\r\n" if "\r\n" in cfg_text else "\n"


def _autosave_split(text):
    """``(editable part, SAVE_CONFIG tail)``; the tail is empty when absent.

    klippy/configfile.py:246-266 splits the file at the AUTOSAVE_HEADER marker
    (":233-237") and treats everything from there down as generated text that must
    not be edited by hand. The marker line klipper itself writes is
    ``#*# <---------------------- SAVE_CONFIG ---------------------->``.
    """
    match = re.search(r"^[ \t]*#.*<[-=]*\s*SAVE_CONFIG\s*[-=]*>", text,
                      re.MULTILINE)
    if match is None:
        return text, ""
    return text[:match.start()], text[match.start():]


def _insert_section(cfg_text, stub, eol):
    """Splice ``stub`` into the editable region, above the autosave block."""
    head, tail = _autosave_split(cfg_text)
    if head and not head.endswith(("\n", "\r")):
        head += eol
    if head and not head.endswith(eol + eol):
        # Exactly one blank line between the last section and ours.
        head += eol
    parts = [head, stub]
    if tail:
        parts.append(tail)
    return "".join(parts)

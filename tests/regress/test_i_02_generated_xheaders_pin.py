"""GROUP integrator / cross-file repair for F2 (hunt finding [8]): the config
half of "the proxy must not flatten every caller to 127.0.0.1".

klipperformac.proxy now writes X-Forwarded-For / X-Real-Ip on every hop (and
drops any copy the browser sent), because the relay always dials Moonraker on
loopback. Upstream only *believes* those headers when the [application]
component's `use_xheaders` is on. That is its default, so this was never broken
- it was only ever *unpinned*: a hand edit, a re-generated config from a fork,
or a future default change would silently turn every proxied caller - this Mac
or a machine across the LAN - back into "the proxy", and then
[authorization].trusted_clients decides nothing about who actually connected.
The generated moonraker.conf now states the dependency out loud.

Two corrections to the assumption this repair started from, both verified
against the pinned pristine checkout (~/.klipperformac/moonraker, 985c1d0,
v0.11.0):

  * there is no `[server] x_forwarded` option in this Moonraker (grep finds
    none); the key lives in `[application] use_xheaders`. Emitting the
    `[server]` spelling would make upstream fail on an unknown option, so this
    file pins the real one and asserts the fake one is never written.
  * 127.0.0.1 / ::1 must STAY in trusted_clients: with xheaders in play,
    upstream's authorization.authenticate_request() first checks that the
    socket peer - our proxy - is a trusted *proxy*, then judges the forwarded
    address. Delete loopback and every proxied request 401s. apply_lan() is
    checked here for exactly that.
"""
import configparser
import re
from pathlib import Path

import pytest

APPLIED = "use_xheaders: True"
# Where the pinned upstream reads the option (components/application.py).
UPSTREAM_APPLICATION = (Path.home() / ".klipperformac" / "moonraker" /
                        "moonraker" / "components" / "application.py")


def parse(text):
    cp = configparser.RawConfigParser(strict=True)
    cp.read_string(text)
    return cp


def without_block(text, name):
    """`text` with the [name] section removed, header and body, comments kept
    wherever they were. Line-based on purpose: the generated file documents the
    relationship between the sections in prose, so searching for the next
    section header as a *substring* would cut in the middle of a sentence."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"\[" + name + r"\]", line):
            skip = True
            continue
        if re.match(r"\[[^\]]+\]\s*$", line):
            skip = False
        if not skip:
            out.append(line)
    return "".join(out)


def generated(stack):
    """MOONRAKER_CFG exactly as ensure_configs() would write it."""
    return stack.configgen.MOONRAKER_CFG.format(
        host="127.0.0.1", moonraker_port=stack.paths.MOONRAKER_PORT,
        uds_address=str(stack.paths.API_SOCKET),
        web_port=stack.paths.WEB_PORT, fluidd_port=stack.paths.FLUIDD_PORT)


def test_the_generated_config_pins_use_xheaders(stack):
    text = generated(stack)
    cp = parse(text)
    assert cp.getboolean("application", "use_xheaders") is True, text
    assert APPLIED in text


def test_the_pin_is_the_option_this_moonraker_actually_reads(stack):
    """The wrong spelling is not a harmless typo: an option upstream does not
    know makes Moonraker refuse the section at startup, and the identity
    handling would silently stay off."""
    text = generated(stack)
    assert "x_forwarded" not in text, "this Moonraker has no x_forwarded option"
    cp = parse(text)
    assert "x_forwarded" not in dict(cp.items("server"))
    if UPSTREAM_APPLICATION.is_file():
        source = UPSTREAM_APPLICATION.read_text(errors="replace")
        assert 'config.getboolean("use_xheaders", True)' in source, \
            "upstream no longer reads this key: re-check the pin"


def test_the_config_is_valid_ini_with_one_of_every_section(stack):
    text = generated(stack)
    cp = parse(text)                        # strict=True rejects duplicates
    for section in ("server", "file_manager", "machine", "application",
                    "authorization"):
        assert cp.has_section(section), text
    assert cp.get("server", "host") == "127.0.0.1"
    # the comments must not have eaten either multi-line value
    assert len(cp.get("authorization", "cors_domains").split()) == 4, text
    assert cp.get("authorization", "trusted_clients").split() == ["127.0.0.1",
                                                                  "::1"], text


def test_ensure_configs_writes_the_pinned_config_for_a_new_install(stack):
    """A fresh install must carry the pin without anyone editing by hand."""
    (stack.paths.KLIPPER / "config").mkdir(parents=True)
    (stack.paths.KLIPPER / "config" /
     stack.configgen.STARTER_EXAMPLE).write_text(
         "[mcu]\nserial: /dev/REPLACE_WITH_YOUR_PRINTER\n")
    created = stack.configgen.ensure_configs()
    assert stack.paths.MOONRAKER_CONF in created
    assert APPLIED in stack.paths.MOONRAKER_CONF.read_text()


@pytest.mark.parametrize("enabled", [True, False])
def test_apply_lan_keeps_the_identity_chain_intact(stack, moonraker_generated,
                                                    enabled):
    """`lan on`/`lan off` may add and drop the RFC1918 ranges, but they must
    not touch the two lines the proxy depends on: the pin, and the loopback
    entries that make the proxy itself a trusted proxy."""
    if not enabled:
        assert stack.configgen.apply_lan(True) is True   # start from LAN on
    assert stack.configgen.apply_lan(enabled) is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    cp = parse(text)
    assert cp.getboolean("application", "use_xheaders") is True, text
    clients = [ln.strip() for ln in
               cp.get("authorization", "trusted_clients").splitlines()
               if ln.strip()]
    assert "127.0.0.1" in clients and "::1" in clients, clients
    assert cp.get("server", "host") == ("0.0.0.0" if enabled else "127.0.0.1")
    cidrs = set(stack.configgen.LAN_CIDRS)
    assert cidrs.issubset(set(clients)) is enabled, clients
    # ...and a second application of the same setting changes nothing.
    assert stack.configgen.apply_lan(enabled) is False
    assert stack.paths.MOONRAKER_CONF.read_text() == text


def test_apply_lan_survives_a_config_without_the_block(stack,
                                                     moonraker_generated):
    """An existing install may predate the pin. apply_lan() must neither crash
    nor invent the block: upstream's own default is True, so absence already
    means the proxy's headers are believed."""
    text = without_block(moonraker_generated, "application")
    assert "[application]" not in text
    stack.paths.MOONRAKER_CONF.write_text(text)
    assert stack.configgen.apply_lan(True) is True
    written = stack.paths.MOONRAKER_CONF.read_text()
    assert "[application]" not in written, "apply_lan must not invent the block"
    assert parse(written).get("server", "host") == "0.0.0.0"


def test_a_user_who_turned_xheaders_off_stays_off(stack, moonraker_generated):
    """We pin on generation, we never re-pin over a deliberate hand edit -
    that is the difference between a template and a silent takeover."""
    text = moonraker_generated.replace(APPLIED, "use_xheaders: False")
    stack.paths.MOONRAKER_CONF.write_text(text)
    stack.configgen.apply_lan(True)
    stack.configgen.sync_moonraker_conf()
    written = stack.paths.MOONRAKER_CONF.read_text()
    assert "use_xheaders: False" in written, written


def test_the_webcam_section_still_lands_after_the_new_block(stack,
                                                            moonraker_generated):
    """configgen appends [webcam maccam] to the file it generated; the new
    [application] block sits before [authorization], so the section bookkeeping
    has to keep counting one of each."""
    assert stack.configgen.ensure_webcam_section() is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert text.count("[webcam ") == 1, text
    assert "[application]" in text and "[authorization]" in text
    parse(text)

"""Managed [webcam maccam] section: upsert/remove/enable must be surgical."""
import configparser

from hypothesis import HealthCheck, given, settings, strategies as st


def parse(path):
    cp = configparser.ConfigParser(strict=False)
    cp.read(path)
    return cp


def count_webcam_sections(path, name="maccam"):
    text = path.read_text().lower()
    return text.count("[webcam " + name.lower() + "]")


def test_ensure_is_idempotent(stack):
    stack.paths.MOONRAKER_CONF.write_text("[server]\nhost: 127.0.0.1\n")
    assert stack.configgen.ensure_webcam_section() is True
    before = stack.paths.MOONRAKER_CONF.read_bytes()
    assert stack.configgen.ensure_webcam_section() is False
    assert stack.paths.MOONRAKER_CONF.read_bytes() == before
    assert count_webcam_sections(stack.paths.MOONRAKER_CONF) == 1


def test_user_keys_survive_enable_and_remove(stack, moonraker_generated):
    stack.configgen.ensure_webcam_section()
    text = stack.paths.MOONRAKER_CONF.read_text()
    text = text.replace("location: printer",
                        "location: printer\ntarget_fps: 12\nflip_horizontal: true")
    stack.paths.MOONRAKER_CONF.write_text(text)
    assert stack.configgen.set_webcam_enabled(False) is True
    assert stack.configgen.set_webcam_enabled(True) is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "target_fps: 12" in text and "flip_horizontal: true" in text
    parse(stack.paths.MOONRAKER_CONF)


def test_enabled_hand_edited_away_is_restored(stack, moonraker_generated):
    stack.configgen.ensure_webcam_section()
    text = stack.paths.MOONRAKER_CONF.read_text()
    text = text.replace("enabled: True", "enabled: ")
    stack.paths.MOONRAKER_CONF.write_text(text)
    assert stack.configgen.set_webcam_enabled(True) is True
    cp = parse(stack.paths.MOONRAKER_CONF)
    assert cp["webcam maccam"]["enabled"].strip() == "True"


def test_set_enabled_never_creates_section(stack):
    stack.paths.MOONRAKER_CONF.write_text("[server]\nhost: 127.0.0.1\n")
    assert stack.configgen.set_webcam_enabled(True) is False
    assert "[webcam" not in stack.paths.MOONRAKER_CONF.read_text()


def test_header_as_last_line_no_newline(stack):
    stack.paths.MOONRAKER_CONF.write_text("[server]\nhost: 127.0.0.1\n\n"
                                          "[webcam maccam]")
    assert stack.configgen.ensure_webcam_section() is True
    assert count_webcam_sections(stack.paths.MOONRAKER_CONF) == 1


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.too_slow,
                                 HealthCheck.function_scoped_fixture])
@given(
    name=st.from_regex(r"[a-z][a-z0-9]{0,8}", fullmatch=True),
    body=st.lists(
        st.tuples(st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True),
                  st.sampled_from(["x", "1", "False", ""])),
        max_size=4),
    crlf=st.booleans(),
)
def test_upsert_never_corrupts_other_sections(name, body, crlf, stack):
    other = "[server]\n" + "".join(k + ": " + v + "\n" for k, v in body)
    stack.paths.MOONRAKER_CONF.write_text(
        other.replace("\n", "\r\n") if crlf else other)
    stack.configgen.ensure_webcam_section(name=name)
    stack.configgen.ensure_webcam_section(name=name)  # idempotent
    cp = parse(stack.paths.MOONRAKER_CONF)
    assert "webcam " + name in cp
    assert count_webcam_sections(stack.paths.MOONRAKER_CONF, name) == 1
    stack.configgen.remove_webcam_section(name=name)
    cp = parse(stack.paths.MOONRAKER_CONF)
    assert "webcam " + name not in cp

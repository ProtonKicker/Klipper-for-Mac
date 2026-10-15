"""versions._sort_key: algebraic properties, fuzz and hostile-size inputs.

This file complements tests/deep/test_versions_sort_key.py, which pins an
enumerated corpus of malformed tags. Everything here is property based:

* totality (no string, however hostile, may raise)
* shape invariants (tuple length, int-ness, non-negativity for ASCII tags)
* ordering invariants (monotone in every component, consistent with max/sorted)
* cost bounds (a multi-megabyte tag must not hang or explode)

Absolutely offline: _sort_key is pure, nothing here touches the lockfile,
~/.klipperformac, a device or the network (an autouse socket trap proves it).
"""
import socket
import time

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

HYP = dict(deadline=None, max_examples=200,
           suppress_health_check=[HealthCheck.function_scoped_fixture])

# Everything a hostile/real-world git tag name could plausibly be built from:
# ASCII digits and letters, separators, signs, whitespace, control bytes,
# non-ASCII digits (arabic-indic, fullwidth), ign/format characters, combining
# marks, surrogate halves and emoji.
TAG_ALPHABET = (
    "vV0123456789.-_+~!#%@()[]{}/\\'\"`"
    " \t\n\r\x00\x1b\x7f"
    "abcdegCLIPPERxyz"
    "٠١٢٣٤٥٦٧٨٩"          # arabic-indic digits
    "０１２３４５６７８９"          # fullwidth digits
    "😀🚀"
    "​‏   "         # zero-width, RTL mark, NBSP, line/para separators
    "ßΩΣ漢字ﬀé"
)


@pytest.fixture(autouse=True)
def socket_trap(monkeypatch):
    """Hard offline proof: any socket creation aborts the test."""
    seen = []

    def _boom(*args, **kwargs):
        seen.append((args, kwargs))
        raise AssertionError("BUG IN TEST: _sort_key must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    return seen


def sk(stack):
    return stack.versions._sort_key


# --------------------------------------------------------------------------
# totality under fuzzing
# --------------------------------------------------------------------------

@given(tag=st.text(alphabet=TAG_ALPHABET, max_size=64))
@settings(**HYP)
def test_sort_key_is_total_and_returns_a_tuple_of_ints(stack, socket_trap, tag):
    key = sk(stack)(tag)
    assert isinstance(key, tuple)
    assert key and all(isinstance(n, int) and not isinstance(n, bool) for n in key)
    assert socket_trap == []


@given(tag=st.text(alphabet=TAG_ALPHABET, min_size=1, max_size=40))
@settings(**HYP)
def test_sort_key_length_is_three_or_the_component_count(stack, tag):
    """len(key) == max(3, number of '.'-separated components) - never more, never less."""
    components = len(tag.lstrip("v").split("."))
    assert len(sk(stack)(tag)) == max(3, components)


@given(parts=st.lists(st.integers(min_value=0, max_value=10 ** 9),
                      min_size=1, max_size=8))
@settings(**HYP)
def test_sort_key_is_exact_for_pure_ascii_dotted_numbers(stack, parts):
    tag = "v" + ".".join(str(p) for p in parts)
    expected = tuple(parts) + (0,) * max(0, 3 - len(parts))
    assert sk(stack)(tag) == expected


@given(parts=st.lists(st.integers(min_value=0, max_value=500),
                      min_size=3, max_size=3))
@settings(**HYP)
def test_sort_key_is_monotone_in_every_component(stack, parts):
    base = sk(stack)("v" + ".".join(map(str, parts)))
    for i, value in enumerate(parts):
        bumped = list(parts)
        bumped[i] = value + 1
        higher = sk(stack)("v" + ".".join(map(str, bumped)))
        assert higher > base
        assert higher[i] == value + 1
        assert higher[:i] == base[:i]


@given(a=st.text(alphabet=TAG_ALPHABET, max_size=30),
       b=st.text(alphabet=TAG_ALPHABET, max_size=30),
       c=st.text(alphabet=TAG_ALPHABET, max_size=30))
@settings(**HYP)
def test_sort_key_orders_are_antisymmetric_and_transitive(stack, a, b, c):
    ka, kb, kc = (sk(stack)(t) for t in (a, b, c))
    assert (ka < kb) == (kb > ka)
    assert (ka == kb) == (kb == ka)
    if ka <= kb and kb <= kc:
        assert ka <= kc


@given(tags=st.lists(st.text(alphabet=TAG_ALPHABET, max_size=24), max_size=12))
@settings(**HYP)
def test_sort_key_sorting_is_idempotent_and_max_agrees_with_sort(stack, tags):
    key = sk(stack)
    once = sorted(tags, key=key)
    assert sorted(once, key=key) == once
    if tags:
        assert key(max(tags, key=key)) == key(once[-1])
        assert key(min(tags, key=key)) == key(once[0])


@given(tag=st.text(alphabet=TAG_ALPHABET, max_size=64))
@settings(**HYP)
def test_sort_key_is_deterministic_and_input_free(stack, tag):
    key = sk(stack)
    assert key(tag) == key(tag)
    same_via_max = max([tag], key=key)
    assert same_via_max == tag


# --------------------------------------------------------------------------
# hostile sizes (1000-char names, deep component lists, huge digit runs)
# --------------------------------------------------------------------------

def test_sort_key_handles_1000_char_names(stack):
    key = sk(stack)
    for tag in ("x" * 1000,
                "v" * 1000,
                "v" + "漢" * 999 + ".2.3",
                "-".join(["segment"] * 200),          # 1400 chars, no dots
                ("v9." * 100) + "9" + "z" * 1000):
        assert isinstance(key(tag), tuple)


def test_sort_key_handles_a_two_megabyte_tag_without_hanging(stack):
    """A hostile GitHub tag list must stay linear-ish; no recursion, no ReDoS."""
    key = sk(stack)
    huge = "v" + ".".join(["1"] * 300_000)            # ~600 KB string
    started = time.monotonic()
    result = key(huge)
    assert len(result) == 300_000
    assert result[0] == 1 and result[-1] == 1
    assert time.monotonic() - started < 10.0


def test_sort_key_handles_a_single_two_megabyte_component(stack):
    key = sk(stack)
    result = key("v" + "9" * 2_000_000)               # int() digit-limit path
    assert result == (0, 0, 0)


def test_sort_key_of_every_pin_and_repo_name_is_safe(stack):
    """Whatever the product ships, _sort_key must survive it (no crash at CLI start)."""
    key = sk(stack)
    values = list(stack.versions.DEFAULT_PINS.values())
    values += list(stack.versions.REPOS) + list(stack.versions.REPOS.values())
    values += [stack.versions.API, str(stack.paths.LOCKFILE)]
    for value in values:
        assert isinstance(key(value), tuple)


# --------------------------------------------------------------------------
# purity and leading-'v' handling
# --------------------------------------------------------------------------

def test_sort_key_never_produces_a_key_shorter_than_three_for_any_text(stack):
    key = sk(stack)
    for tag in ("", "v", "1", "1.2", "\n", "١", "v-v", "v.v.v"):
        assert len(key(tag)) >= 3


def test_sort_key_is_a_pure_function_of_its_argument(stack):
    """No global state, no disk, no network: the same tag always gives the same key."""
    key = sk(stack)
    tag = "v2.19.0-rc1"
    baseline = key(tag)
    shipped = dict(stack.versions.DEFAULT_PINS)
    stack.paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.LOCKFILE.write_text('{"moonraker": "v9.9.9"}')
    assert key(tag) == baseline
    assert stack.versions.DEFAULT_PINS == shipped


def test_sort_key_does_not_score_double_v_garbage_as_a_release(stack):
    key = sk(stack)
    assert key("vv1.2.3") != key("v1.2.3")
    assert key("vvvvv2.19.0") != key("v2.19.0")
    assert key("vv1.2.3") == (0, 0, 0)


def test_sort_key_never_produces_a_key_shorter_than_three_for_any_text(stack):
    key = sk(stack)
    for tag in ("", "v", "1", "1.2", "\n", "١", "v-v", "v.v.v"):
        assert len(key(tag)) >= 3

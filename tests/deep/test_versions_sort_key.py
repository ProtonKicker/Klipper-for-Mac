"""versions._sort_key: total-function behaviour on malformed / hostile / unicode tags.

offline only: no lockfile writes, no network, no ~/.klipperformac.
"""
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# _sort_key is a pure function: one 'stack' instance per hypothesis test is
# intentional (nothing is written to disk), so the fixture-reuse check is noise.
HYP = dict(deadline=None, max_examples=300,
           suppress_health_check=[HealthCheck.function_scoped_fixture])

MALFORMED = [
    "",
    " ",
    "v",
    "vv",
    "v" * 5000,
    "garbage",
    "v1.x",
    "v1.x.y",
    "1.x",
    "v1..2",
    "v1.2.",
    ".1.2.3",
    "..",
    "v1.2.3.4.5.6",
    "v2.19.0-rc1",
    "v2.19.0+build.9",
    "release-1.2.3",
    "v-1.-2.-3",
    "v1.2.3\n",
    "v1.\t2. 3",
    "v1.2.3garbage",
    "\n",
    "\x00",
    "v1\x00.2.3",
    "v\U0001f600.2.3",
    "v0.0.0",
    "0" * 4300,
    "9" * 5000,             # > int() digit limit -> ValueError, must be swallowed
    "9" * 100_000,
    "v1_0.2.3",
    "١.٢.٣",               # arabic-indic digits
    "v１.２.３",             # fullwidth digits
    "v1.2.３",
    "ｖ1.2.3",
    "v1.2.3"[::-1],
    "v2.3.4" * 250,         # 1500+ chars
    "x" * 1000,             # 1000-char name
    "v" + "x" * 999 + ".1.2",
    jsonish := '{"name": "v1.2.3"}',
    "[1,2,3]",
    "v1.2.3\x00\x00",
    "\u200b" + "v1.2.3",
]


def _key(ns, tag):
    return ns.versions._sort_key(tag)


@pytest.mark.parametrize("tag", MALFORMED)
def test_sort_key_is_total_on_strings(stack, tag):
    """No string input may raise: _sort_key is used on unvalidated upstream tags."""
    key = _key(stack, tag)
    assert isinstance(key, tuple)
    assert len(key) >= 3
    assert all(isinstance(n, int) and not isinstance(n, bool) for n in key)


@pytest.mark.parametrize("tag", MALFORMED)
def test_sort_key_deterministic(stack, tag):
    key = _key(stack, tag)
    assert _key(stack, tag) == key
    assert max([tag, tag], key=lambda t: _key(stack, t)) == tag


def test_sort_key_parses_happy_path(stack):
    sk = stack.versions._sort_key
    assert sk("v1.2.3") == (1, 2, 3)
    assert sk("v0.11.0") == (0, 11, 0)
    assert sk("1.2.3") == (1, 2, 3)
    assert sk("v2.19") == (2, 19, 0)
    assert sk("v3") == (3, 0, 0)


def test_sort_key_orders_realistic_tags(stack):
    sk = stack.versions._sort_key
    ordered = ["v0.9.0", "v0.10.0", "v0.11.0", "v1.0.0", "v10.0.0", "v10.10.10"]
    keys = [sk(t) for t in ordered]
    assert keys == sorted(keys), "semantic order must be monotonic"
    assert max(ordered, key=sk) == "v10.10.10"
    # numeric, not lexicographic: 10 > 9 in every component
    assert sk("v1.2.10") > sk("v1.2.9")


def test_sort_key_pads_short_tags_to_three_components(stack):
    sk = stack.versions._sort_key
    assert len(sk("")) == 3
    assert len(sk("v1")) == 3
    assert len(sk("v1.2")) == 3
    assert sk("v1") == sk("v1.0") == sk("v1.0.0")


def test_sort_key_leading_v_is_optional(stack):
    sk = stack.versions._sort_key
    assert sk("v1.2.3") == sk("1.2.3")


def test_sort_key_huge_tag_does_not_hang_or_raise(stack):
    sk = stack.versions._sort_key
    big = "v" + ".".join(["9" * 4000] * 50)          # ~200 KB single string
    key = sk(big)
    assert len(key) == 50


@given(tag=st.text(max_size=120))
@settings(**HYP)
def test_sort_key_never_raises_for_any_text(stack, tag):
    key = stack.versions._sort_key(tag)
    assert isinstance(key, tuple) and len(key) >= 3
    assert all(isinstance(n, int) for n in key)


@given(parts=st.lists(st.integers(min_value=0, max_value=10_000),
                      min_size=1, max_size=5))
@settings(**HYP)
def test_sort_key_matches_numeric_tuple_for_plain_dotted_numbers(stack, parts):
    tag = "v" + ".".join(str(p) for p in parts)
    key = stack.versions._sort_key(tag)
    expected = tuple(parts) + (0,) * max(0, 3 - len(parts))
    assert key == expected


@given(a=st.lists(st.integers(min_value=0, max_value=99), min_size=3, max_size=3),
       b=st.lists(st.integers(min_value=0, max_value=99), min_size=3, max_size=3))
@settings(**HYP)
def test_sort_key_ordering_agrees_with_componentwise_order(stack, a, b):
    sk = stack.versions._sort_key
    ka, kb = sk("v" + ".".join(map(str, a))), sk("v" + ".".join(map(str, b)))
    assert (ka > kb) == (tuple(a) > tuple(b))
    assert (ka == kb) == (tuple(a) == tuple(b))


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _sort_key feeds raw tag components to int(), which accepts unicode "
    "digits, '_' digit separators, surrounding whitespace, a '+'/'-' sign and "
    "non-breaking spaces, so 'v1_0.2.3' is read as 10.x.y and 'v1.-2.3' as a "
    "negative component instead of being rejected as a non-version tag"))
def test_sort_key_rejects_non_ascii_digit_and_separator_forms(stack):
    """A tag is not a version unless every component is pure ASCII 0-9."""
    sk = stack.versions._sort_key
    # underscores must not silently merge into a bigger number
    assert sk("v1_0.2.3") != sk("v10.2.3")
    # signs must not produce negative components (they invert the ordering)
    assert sk("v1.-2.3") == sk("v0.0.0") or sk("v1.-2.3") >= (0, 0, 0)
    # unicode digits are not ASCII digits
    assert sk("١.٢.٣") == (0, 0, 0)
    assert sk("v１.２.３") == (0, 0, 0)
    # whitespace inside/around a component must not be tolerated
    assert sk("v 1.2.3") == (0, 0, 0)


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _sort_key maps a too-long digit run (>4300 digits, int() limit) and "
    "every other unparseable form to the same (0,0,0) key as the empty string, so "
    "garbage tags tie with real v0.x tags and max() can hand back a garbage tag"))
def test_sort_key_distinguishes_garbage_from_real_tags(stack):
    sk = stack.versions._sort_key
    garbage_keys = {t: sk(t) for t in ("", "garbage", "vxyz", "9" * 5000, "[]")}
    assert all(k != (0, 0, 0) for k in garbage_keys.values()), \
        "unparseable tags must not collide with the real tag 'v0.0.0'"
    assert sk("v0.0.0") == (0, 0, 0)
    # max() over a list where a garbage tag comes first and ties with v0.0.0
    # silently picks the garbage tag
    assert max(["v0.0.0", "garbage-ish"], key=sk) == "v0.0.0"


def test_sort_key_rejects_non_string_input(stack):
    """Documented domain is str; other types must not be silently mangled."""
    sk = stack.versions._sort_key
    for bad in (None, 1, 1.5, b"v1.2.3", ["v1.2.3"], {"v": 1}):
        with pytest.raises((TypeError, AttributeError)):
            sk(bad)

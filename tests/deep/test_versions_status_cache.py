"""versions.cached_check: the status path never pays GitHub latency twice.

The perf hunt's headline finding - 'klipperformac status' made 3 serial
HTTPS calls to GitHub on EVERY invocation (seconds of wall time, rate-limit
exposure, offline noise). status now reads a 6-hour disk cache; 'update'
stays live because the human explicitly asked.
"""
import json
import time

import pytest


def _cache_file(stack):
    return stack.paths.DATA / "cache" / "update-check.json"


def install_fake_checker(stack, monkeypatch, results):
    """results: list of (updates, errors) pairs, consumed per live check."""
    calls = []

    def fake():
        calls.append(time.time())
        return results.pop(0) if results else ([], [])
    monkeypatch.setattr(stack.versions, "check_updates", fake)
    return calls


def test_fresh_cache_skips_the_network(stack, monkeypatch):
    fake_calls = install_fake_checker(
        stack, monkeypatch, [([("x", "1", "2")], ["boom"])])
    u, e = stack.versions.cached_check()
    assert u == [("x", "1", "2")] and e == ["boom"] and len(fake_calls) == 1
    u2, e2 = stack.versions.cached_check()
    assert (u2, e2) == (u, e)
    assert len(fake_calls) == 1, "second status read must not touch GitHub"


def test_stale_cache_refetches(stack, monkeypatch):
    fake_calls = install_fake_checker(stack, monkeypatch,
                                      [([], []), ([], [])])
    stack.versions.cached_check()
    f = _cache_file(stack)
    data = json.loads(f.read_text())
    data["checked_at"] = time.time() - 7 * 3600
    f.write_text(json.dumps(data))
    stack.versions.cached_check()
    assert len(fake_calls) == 2


def test_corrupt_cache_is_invisible(stack, monkeypatch):
    install_fake_checker(stack, monkeypatch,
                         [([("c", "v1", "v2")], [])])
    _cache_file(stack).parent.mkdir(parents=True, exist_ok=True)
    _cache_file(stack).write_bytes(b"\xff{not json")
    u, e = stack.versions.cached_check()
    assert u == [("c", "v1", "v2")]


@pytest.mark.parametrize("value", [None, 5, "nope", [],
                                   {"checked_at": "x"}])
def test_malformed_shapes_never_crash(stack, monkeypatch, value):
    install_fake_checker(stack, monkeypatch,
                         [([("k", "a", "b")], [])])
    f = _cache_file(stack)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("" if value is None else json.dumps(value))
    u, e = stack.versions.cached_check()
    assert u == [("k", "a", "b")]


def test_network_error_falls_back_to_stale_cache(stack, monkeypatch):
    fake_calls = install_fake_checker(
        stack, monkeypatch,
        [([("old", "1", "2")], []), ([], ["api down"])])
    stack.versions.cached_check()          # populate
    f = _cache_file(stack)
    data = json.loads(f.read_text())
    data["checked_at"] = time.time() - 7 * 3600   # make stale
    f.write_text(json.dumps(data))
    u, e = stack.versions.cached_check()   # live check errors -> stale wins
    assert u == [("old", "1", "2")] and e == []
    on_disk = json.loads(f.read_text())
    assert on_disk["updates"] == [["old", "1", "2"]], (
        "a failed live check must never clobber previously known facts")
    assert len(fake_calls) == 2

"""The soft_scan sweep must not wipe last-good flags when a book is briefly
unreachable (a DNS blip that hit all three books used to zero out the tab)."""
from datetime import datetime, timedelta, timezone

from src.app import _carry_stale_flags, _flag_expired

NOW = datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(hours=3)).isoformat()
PAST = (NOW - timedelta(minutes=5)).isoformat()


def _flag(book, sport, kind="soccer_fair", start=FUTURE, **kw):
    return {"book": book, "sport": sport, "kind": kind, "outcome": "htft:2/2",
            "book_event_id": "e1", "severity": 5.0, "start_time": start, **kw}


def test_all_scanners_fail_keeps_previous():
    prev = [_flag("cb", "soccer"), _flag("betlive", "soccer"), _flag("liderbet", "basketball")]
    failed = {("cb", "soccer"), ("betlive", "soccer"), ("liderbet", "basketball")}
    out = _carry_stale_flags(prev, [], failed, NOW, stale_max=3 * 3600)
    assert len(out) == 3 and all(f["stale"] for f in out)   # nothing wiped


def test_partial_failure_keeps_failed_replaces_succeeded():
    prev = [_flag("cb", "soccer"), _flag("betlive", "soccer")]
    fresh = [_flag("betlive", "soccer", severity=9.0)]      # betlive re-scanned fine
    failed = {("cb", "soccer")}                              # only CB unreachable
    out = _carry_stale_flags(prev, fresh, failed, NOW, stale_max=3 * 3600)
    cb = [f for f in out if f["book"] == "cb"]
    bl = [f for f in out if f["book"] == "betlive"]
    assert len(cb) == 1 and cb[0]["stale"]                  # CB carried (stale)
    assert len(bl) == 1 and not bl[0].get("stale") and bl[0]["severity"] == 9.0  # fresh only


def test_success_with_zero_flags_clears():
    # a scanner that ran fine and found nothing must NOT resurrect old flags.
    prev = [_flag("cb", "soccer")]
    out = _carry_stale_flags(prev, [], failed=set(), now=NOW, stale_max=3 * 3600)
    assert out == []


def test_kicked_off_stale_flag_dropped():
    prev = [_flag("cb", "soccer", start=PAST)]              # game already started
    out = _carry_stale_flags(prev, [], {("cb", "soccer")}, NOW, stale_max=3 * 3600)
    assert out == []


def test_stale_too_long_dropped():
    old = _flag("cb", "soccer", stale_since=(NOW - timedelta(hours=5)).isoformat())
    assert _flag_expired(old, NOW, stale_max=3 * 3600)
    fresh = _flag("cb", "soccer", stale_since=(NOW - timedelta(minutes=30)).isoformat())
    assert not _flag_expired(fresh, NOW, stale_max=3 * 3600)

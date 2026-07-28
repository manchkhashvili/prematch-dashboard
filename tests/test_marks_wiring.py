"""Every page that renders a Mark button must also load and bind marks.

Shipped 2026-07-28 with anomalies.html half-wired: it rendered the buttons and
row classes but never called Marks.load() or Marks.bind(), so nothing
highlighted and clicking did nothing. The failure is silent — the page looks
correct — so it needs a static check rather than trusting review.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGES = sorted(p for p in STATIC.glob("*.html")
               if "marks.js" in p.read_text(encoding="utf-8"))


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_some_pages_actually_use_marks():
    """Guard the guard: if the glob stops matching, every test below would
    vacuously pass."""
    assert PAGES, "no page includes marks.js — did the include get renamed?"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_that_renders_a_mark_button_also_loads_marks(page):
    t = _text(page)
    if "Marks.buttonHTML(" not in t:
        pytest.skip(f"{page.name} includes marks.js but renders no button")
    assert "Marks.load()" in t, (
        f"{page.name} renders a Mark button but never calls Marks.load() — "
        "buttons will always read 'Mark' and nothing will highlight")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_that_renders_a_mark_button_also_binds_a_handler(page):
    t = _text(page)
    if "Marks.buttonHTML(" not in t:
        pytest.skip(f"{page.name} includes marks.js but renders no button")
    assert "Marks.bind(" in t, (
        f"{page.name} renders a Mark button but never calls Marks.bind() — "
        "clicking it does nothing")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_table_holding_a_mark_button_is_bound(page):
    """anomalies.html has two tables; binding only one leaves the other dead."""
    t = _text(page)
    if "Marks.buttonHTML(" not in t:
        pytest.skip(f"{page.name} renders no button")
    bound = set(re.findall(r'Marks\.bind\(document\.getElementById\("([^"]+)"\)', t))
    bodies = set(re.findall(r'<tbody id="([^"]+)"', t))
    # Only tbodies that actually render rows containing a Mark button matter,
    # and we cannot tell which from static text — so require that a page with
    # N tbodies binds at least one, and that every bound id is a real tbody.
    assert bound, f"{page.name}: no tbody bound"
    unknown = bound - bodies
    assert not unknown, f"{page.name}: bound non-existent tbody ids {unknown}"
    if len(bodies) > 1:
        assert bound == bodies, (
            f"{page.name} has tbodies {sorted(bodies)} but only bound "
            f"{sorted(bound)} — the unbound one's Mark buttons are dead")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_marks_js_is_included_before_use(page):
    """marks.js must be a plain (non-defer) script so window.Marks exists by
    the time the page's inline script runs at end of body."""
    t = _text(page)
    m = re.search(r'<script src="/marks\.js[^"]*"([^>]*)>', t)
    assert m, f"{page.name}: no marks.js include"
    assert "defer" not in m.group(1), (
        f"{page.name}: marks.js is deferred — inline render code would run "
        "before window.Marks exists")

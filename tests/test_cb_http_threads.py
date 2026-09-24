"""Every CrystalBet HTTP call runs on its session's own thread.

Not a tidiness rule — a file-descriptor leak that took the whole dashboard
down. curl_cffi's Session defaults to `use_thread_local_curl=True`, so
`session.curl` mints a NEW Curl handle the first time each thread touches it
and `Session.close()` frees only the calling thread's. The wrappers used to
run on `asyncio.to_thread`, i.e. on whichever of the default executor's ~12
threads was free, so every sport session collected a handle per worker thread
and closing it orphaned all but one.

Measured on the owner's box, 1 h 23 m uptime: 72 sockets to crystalbet.com,
~50 in state CLOSED, against a soft limit of 256 fds the process had already
reached (255 used, highest fd 254). Past that, accept() returns EMFILE and
uvicorn resets every incoming connection — the dashboard stops answering and
the Anomalies tab shows "error loading anomalies" until a restart.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from src.scrapers import cb_http

SRC = Path(cb_http.__file__).read_text()


def _stub(sess, seen):
    """Make a session answer without network, recording the thread each call
    lands on."""
    sess.s = object()
    sess.warmed_at = 1e18                      # never re-warm
    sess._list_panel = lambda: (seen.append(threading.current_thread().name) or "<ok>")
    sess.expand_detail_raw = lambda gid: (seen.append(threading.current_thread().name) or "<ok>")
    sess.expand_detail_html = lambda gid: (seen.append(threading.current_thread().name) or "<ok>")
    sess.fetch_list_html = lambda: (seen.append(threading.current_thread().name) or "<ok>")
    sess.fetch_list_raw = lambda: (seen.append(threading.current_thread().name) or "<ok>")
    return sess


@pytest.fixture
def sess(monkeypatch):
    s = cb_http.CbHttpSession(999)
    monkeypatch.setattr(cb_http, "_get_session", lambda sport_id: s)
    monkeypatch.setattr(cb_http, "_ensure_warm_sync", lambda _s: None)
    yield s
    s.shutdown()


def test_every_wrapper_lands_on_the_sessions_own_thread(sess):
    seen: list[str] = []
    _stub(sess, seen)

    async def go():
        await cb_http.fetch_list_raw(999)
        await cb_http.expand_detail_raw(999, "g1")

    asyncio.run(go())
    assert seen, "nothing ran"
    assert len(set(seen)) == 1, f"calls spread across threads: {set(seen)}"
    assert seen[0].startswith("cb-http-999"), (
        f"ran on {seen[0]!r}, not the session's own thread — every extra "
        f"thread that touches this session strands a curl handle"
    )


def test_concurrent_calls_do_not_interleave(sess):
    """CbHttpSession IS ASP.NET postback state: `fields` carries __VIEWSTATE
    and every _post rewrites it. Two threads in here at once kill the
    session. One worker serialises them."""
    inside = []
    peak = 0

    def busy():
        nonlocal peak
        inside.append(1)
        peak = max(peak, len(inside))
        threading.Event().wait(0.02)
        inside.pop()
        return "<ok>"

    sess.s = object()
    sess.warmed_at = 1e18
    sess._list_panel = busy
    sess.expand_detail_raw = lambda gid: busy()

    async def go():
        await asyncio.gather(*[cb_http.fetch_list_raw(999) for _ in range(4)],
                             *[cb_http.expand_detail_raw(999, "g") for _ in range(4)])

    asyncio.run(go())
    assert peak == 1, f"{peak} calls were inside the session at once"


def test_shutdown_closes_the_handle_on_the_owning_thread(sess):
    closed_on: list[str] = []

    class FakeCurlSession:
        def close(self):
            closed_on.append(threading.current_thread().name)

    sess.s = FakeCurlSession()
    sess.shutdown()
    assert closed_on, "the curl session was never closed"
    assert closed_on[0].startswith("cb-http-999"), (
        f"closed from {closed_on[0]!r}. curl_cffi frees only the CALLING "
        f"thread's handle, so closing from elsewhere strands the real one "
        f"and mints a second handle just to close it"
    )


def test_reset_session_retires_the_thread(monkeypatch):
    monkeypatch.setattr(cb_http, "_sessions", {})
    s = cb_http._get_session(998)
    name = s._ex._thread_name_prefix
    s.submit(lambda: None).result()
    live = {t.name for t in threading.enumerate()}
    assert any(n.startswith(name) for n in live), "the session thread never started"
    cb_http.reset_session(998)
    s._ex.shutdown(wait=True)
    assert not any(t.name.startswith(name) and t.is_alive()
                   for t in threading.enumerate()), "the session thread outlived its session"


def test_no_wrapper_falls_back_to_the_shared_executor():
    """asyncio.to_thread uses the default pool — a different thread each time,
    which is the whole bug. A new wrapper must not reintroduce it."""
    code = [l for l in SRC.splitlines()
            if "asyncio.to_thread(" in l and not l.lstrip().startswith("#")]
    assert not code, (
        f"a CB wrapper runs on the shared executor again: {code}. "
        f"Use asyncio.wrap_future(sess.submit(run))."
    )
    assert SRC.count("sess.submit(run)") >= 4


def test_the_thread_local_default_is_what_makes_this_necessary():
    """If curl_cffi ever stops handing out a handle per thread, this whole
    arrangement can be simplified — so assert the reason still holds."""
    from curl_cffi.requests import Session
    import inspect
    assert "use_thread_local_curl: bool = True" in inspect.getsource(Session.__init__)
    assert "self.curl.close()" in inspect.getsource(Session.close)


# ── the UI side of the same incident ─────────────────────────────────────────

@pytest.mark.parametrize("page", ["anomalies.html", "new_inconsistencies.html"])
def test_a_failed_poll_clears_both_tables(page):
    """The catch used to write only the ladder tbody, so the consistency table
    kept its first-load "loading…" placeholder for the life of the page. A
    server that resets connections then looks like a frozen dashboard."""
    src = (Path(__file__).resolve().parent.parent / "static" / page).read_text()
    i = src.index("function renderFetchError")
    body = src[i:src.index("\nasync function fetchAndRender", i)]
    for tbody in ("anom-body", "cons-body"):
        assert tbody in body, f"{page}: a failed poll leaves #{tbody} stale"
    assert "retrying" in body, f"{page}: does not say it will retry"


@pytest.mark.parametrize("page", ["anomalies.html", "new_inconsistencies.html"])
def test_the_catch_goes_through_the_shared_renderer(page):
    src = (Path(__file__).resolve().parent.parent / "static" / page).read_text()
    i = src.index("async function fetchAndRender")
    body = src[i:src.index("\n}", src.index("} catch (e) {", i))]
    assert "renderFetchError(" in body
    assert "error loading" not in body, (
        f"{page}: the old single-table error path is back"
    )


def test_the_fd_cost_does_not_scale_with_the_thread_pool():
    """The measurement, end to end, against a local socket.

    A curl_cffi Session used from a 12-thread pool holds 24 fds per session
    and never gives them back, because asyncio's default executor retires no
    threads — so the thread-local handles, and their pooled sockets, outlive
    every close(). Five sport sessions is 120 fds; the process ceiling is 256.
    Pinned to one thread the same work holds none.
    """
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from curl_cffi.requests import Session

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    fds = lambda: len(os.listdir("/dev/fd"))
    try:
        shared = ThreadPoolExecutor(max_workers=8)
        list(shared.map(lambda _: None, range(8)))        # start every thread
        base = fds()
        s = Session()
        list(shared.map(lambda _: s.get(url, timeout=10), range(8)))
        s.close()
        spread = fds() - base
        shared.shutdown(wait=True)

        base = fds()
        s2 = Session()
        ex = ThreadPoolExecutor(max_workers=1)
        for _ in range(8):
            ex.submit(s2.get, url, timeout=10).result()
        ex.submit(s2.close).result()
        pinned = fds() - base
        ex.shutdown(wait=True)
    finally:
        srv.shutdown()

    assert pinned <= 2, f"the pinned session still holds {pinned} fds"
    assert spread > pinned, (
        f"expected the pool-spread session to hold more fds than the pinned "
        f"one (got {spread} vs {pinned}) — if curl_cffi stopped keeping a "
        f"handle per thread, CbHttpSession's own thread is no longer needed"
    )

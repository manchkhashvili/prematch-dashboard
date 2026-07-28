"""dns_pin: raw-UDP A lookup + curl RESOLVE pinning.

Regression cover for the 2026-07-28 CB outage: macOS getaddrinfo wedged on
www.crystalbet.com (30s hang → "Could not resolve host") while the same
nameserver answered a raw UDP query instantly. All tests here are offline —
the socket layer is stubbed.
"""
from __future__ import annotations

import struct

import pytest

from src.scrapers import dns_pin


def _wire_response(ip: str, *, qname: str = "www.example.com", ancount: int = 1,
                   rtype: int = 1) -> bytes:
    """Build a minimal DNS answer the parser should accept."""
    out = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, ancount, 0, 0)
    for part in qname.split("."):
        out += bytes([len(part)]) + part.encode()
    out += b"\x00" + struct.pack(">HH", 1, 1)          # QTYPE=A QCLASS=IN
    for _ in range(ancount):
        out += b"\xc0\x0c"                              # compressed NAME ptr
        rdata = bytes(int(o) for o in ip.split("."))
        out += struct.pack(">HHIH", rtype, 1, 300, len(rdata)) + rdata
    return out


class _FakeSock:
    """Stands in for socket.socket; records the server it was asked to query."""

    sent_to: list[str] = []

    def __init__(self, payload: bytes | None = None, exc: Exception | None = None):
        self._payload, self._exc = payload, exc

    def settimeout(self, _t): pass

    def sendto(self, _q, addr):
        type(self).sent_to.append(addr[0])
        if self._exc:
            raise self._exc

    def recvfrom(self, _n):
        return self._payload, None

    def close(self): pass


@pytest.fixture(autouse=True)
def _reset():
    _FakeSock.sent_to = []
    yield


def _patch_socket(monkeypatch, payload=None, exc=None):
    monkeypatch.setattr(dns_pin.socket, "socket",
                        lambda *a, **k: _FakeSock(payload, exc))


# ── wire parsing ─────────────────────────────────────────────────────────────

def test_dns_a_parses_a_record(monkeypatch):
    _patch_socket(monkeypatch, _wire_response("91.233.15.162"))
    assert dns_pin.dns_a("1.1.1.1", "www.example.com") == "91.233.15.162"


def test_dns_a_skips_non_a_records(monkeypatch):
    """A CNAME-only answer must yield None, not a garbage 'IP'."""
    _patch_socket(monkeypatch, _wire_response("1.2.3.4", rtype=5))
    assert dns_pin.dns_a("1.1.1.1", "www.example.com") is None


def test_dns_a_handles_empty_answer(monkeypatch):
    _patch_socket(monkeypatch, _wire_response("1.2.3.4", ancount=0))
    assert dns_pin.dns_a("1.1.1.1", "www.example.com") is None


# ── nameserver ordering ──────────────────────────────────────────────────────

def test_nameservers_always_offer_public_fallback():
    """The wedged local resolver must never be the only option."""
    servers = dns_pin.nameservers()
    assert "1.1.1.1" in servers and "8.8.8.8" in servers


# ── resolve(): caching + failover ────────────────────────────────────────────

def test_resolve_caches_within_ttl(monkeypatch):
    _patch_socket(monkeypatch, _wire_response("91.233.15.162"))
    pin = dns_pin.DnsPin("www.crystalbet.com")
    assert pin.resolve() == "91.233.15.162"
    first = len(_FakeSock.sent_to)
    assert pin.resolve() == "91.233.15.162"
    assert len(_FakeSock.sent_to) == first, "second call should hit the cache"


def test_resolve_tries_next_server_on_failure(monkeypatch):
    """A dead first nameserver must not stop us reaching a working one."""
    calls: list[str] = []

    def fake_dns_a(server, host, timeout=3.0):
        calls.append(server)
        if len(calls) == 1:
            raise OSError("timed out")
        return "91.233.15.162"

    monkeypatch.setattr(dns_pin, "dns_a", fake_dns_a)
    monkeypatch.setattr(dns_pin, "nameservers", lambda: ["192.168.0.2", "1.1.1.1"])
    pin = dns_pin.DnsPin("www.crystalbet.com")
    assert pin.resolve() == "91.233.15.162"
    assert calls == ["192.168.0.2", "1.1.1.1"]


def test_resolve_returns_stale_ip_when_all_servers_fail(monkeypatch):
    """Stale beats nothing: a total DNS outage shouldn't unpin a working IP."""
    pin = dns_pin.DnsPin("www.crystalbet.com")
    monkeypatch.setattr(dns_pin, "dns_a", lambda *a, **k: "91.233.15.162")
    assert pin.resolve() == "91.233.15.162"
    pin._ts = 0.0                                    # force TTL expiry
    monkeypatch.setattr(dns_pin, "dns_a",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert pin.resolve() == "91.233.15.162"


def test_resolve_returns_none_when_never_resolved(monkeypatch):
    monkeypatch.setattr(dns_pin, "dns_a",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert dns_pin.DnsPin("www.crystalbet.com").resolve() is None


# ── pin(): curl wiring + fail-safety ─────────────────────────────────────────

class _FakeCurl:
    def __init__(self): self.opts: list[tuple] = []
    def setopt(self, opt, val): self.opts.append((opt, val))


class _FakeSession:
    def __init__(self): self.curl = _FakeCurl()


def test_pin_sets_resolve_entry(monkeypatch):
    monkeypatch.setattr(dns_pin, "dns_a", lambda *a, **k: "91.233.15.162")
    monkeypatch.setattr(dns_pin._tls, "pinned", {}, raising=False)
    sess = _FakeSession()
    dns_pin.DnsPin("www.crystalbet.com", 443).pin(sess)
    vals = [v for _o, v in sess.curl.opts]
    assert ["www.crystalbet.com:443:91.233.15.162"] in vals


def test_pin_drops_stale_entry_when_ip_rotates(monkeypatch):
    """curl keeps the old mapping unless we explicitly remove it with '-host:port'."""
    monkeypatch.setattr(dns_pin._tls, "pinned", {}, raising=False)
    pin = dns_pin.DnsPin("www.crystalbet.com", 443)
    monkeypatch.setattr(dns_pin, "dns_a", lambda *a, **k: "91.233.15.162")
    pin.pin(_FakeSession())

    pin._ts = 0.0
    monkeypatch.setattr(dns_pin, "dns_a", lambda *a, **k: "203.0.113.7")
    sess = _FakeSession()
    pin.pin(sess)
    entries = sess.curl.opts[-1][1]
    assert entries[0] == "-www.crystalbet.com:443"
    assert entries[1] == "www.crystalbet.com:443:203.0.113.7"


def test_pin_is_noop_when_unresolvable(monkeypatch):
    """Normal networks / total DNS failure: leave DNS to curl, never raise."""
    monkeypatch.setattr(dns_pin, "dns_a",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    sess = _FakeSession()
    dns_pin.DnsPin("www.crystalbet.com").pin(sess)
    assert sess.curl.opts == []


def test_pin_swallows_curl_errors(monkeypatch):
    """Pinning must never be able to break a request."""
    monkeypatch.setattr(dns_pin, "dns_a", lambda *a, **k: "91.233.15.162")

    class Boom:
        curl = property(lambda self: (_ for _ in ()).throw(RuntimeError("no curl")))

    dns_pin.DnsPin("www.crystalbet.com").pin(Boom())      # must not raise


# ── the CB call sites are actually wired ─────────────────────────────────────

def test_cb_http_pins_its_host():
    from src.scrapers import cb_http
    assert cb_http._PIN.host == "www.crystalbet.com"
    assert cb_http._PIN.port == 443


def test_cb_http_pins_before_every_request():
    """warm()'s GET and _post()'s POST must both pin — a missed site re-wedges."""
    import inspect
    from src.scrapers import cb_http
    for fn in (cb_http.CbHttpSession.warm, cb_http.CbHttpSession._post):
        src = inspect.getsource(fn)
        assert "_PIN.pin(" in src, f"{fn.__name__} does not pin before requesting"

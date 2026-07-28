"""Host→IP pinning that bypasses the OS resolver.

macOS mDNSResponder (and other OS resolvers) can WEDGE on gambling domains when
the ISP interferes with DNS: getaddrinfo hangs its full ~30s timeout and then
fails with "Could not resolve host", even though `nslookup`/public resolvers
answer instantly and the server serves fine when hit by IP.

Seen twice now, on two different books:
  * 1xbet-ge.com       (2026-07-04) — every 1xbet call stalled, x_count=0
  * www.crystalbet.com (2026-07-28) — every CB poll burned 30s, cb rows=0

Both times the bare domain still resolved while the `www.` host did not, so it
is a per-name failure in the local resolver, not the site being down.

We sidestep it by resolving the host ourselves over raw UDP (never touching
getaddrinfo) and pinning host→IP on the curl handle via CURLOPT_RESOLVE.
Fail-safe: if our own query can't resolve either, we leave DNS to curl, so
normal networks are unaffected.

Usage — call pin() before each request, on the same thread that will make it:

    from src.scrapers import dns_pin
    _PIN = dns_pin.DnsPin("www.crystalbet.com", 443)
    ...
    _PIN.pin(session)
    session.get(url, ...)

pin() must be re-applied per request rather than once per session: curl_cffi
keeps a curl handle per thread, and the pollers run in worker threads, so a
session object touched from a new thread carries an unpinned handle.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

DNS_TTL = 600.0          # site IPs are stable but may rotate
QUERY_TIMEOUT = 3.0

_tls = threading.local()


def nameservers() -> list[str]:
    """Configured resolvers first, then public ones as the escape hatch."""
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    servers.append(line.split()[1])
    except Exception:
        pass
    for pub in ("1.1.1.1", "8.8.8.8"):   # local resolver may be wedged for us
        if pub not in servers:
            servers.append(pub)
    return servers


def dns_a(server: str, host: str, timeout: float = QUERY_TIMEOUT) -> str | None:
    """One raw UDP A-record query. Bypasses getaddrinfo entirely."""
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    for part in host.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)   # QTYPE=A, QCLASS=IN
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    ancount = struct.unpack(">H", data[6:8])[0]
    i = 12
    while data[i]:                             # skip the question's QNAME
        i += 1 + data[i]
    i += 5                                      # null byte + QTYPE + QCLASS
    for _ in range(ancount):
        i += 2                                  # compressed NAME pointer
        typ, _cls, _ttl, rdl = struct.unpack(">HHIH", data[i:i + 10]); i += 10
        if typ == 1 and rdl == 4:               # A record
            return ".".join(str(b) for b in data[i:i + 4])
        i += rdl
    return None


class DnsPin:
    """Resolve-and-pin for one host:port. Thread-safe; cache shared per instance."""

    def __init__(self, host: str, port: int = 443):
        self.host = host
        self.port = port
        self._ip: str | None = None
        self._ts = 0.0
        self._lock = threading.Lock()

    def resolve(self) -> str | None:
        """Cached A-record lookup. Returns the last good IP if every server fails."""
        now = time.time()
        with self._lock:
            if self._ip and now - self._ts < DNS_TTL:
                return self._ip
        for srv in nameservers():
            try:
                ip = dns_a(srv, self.host)
            except Exception:
                continue
            if ip:
                with self._lock:
                    if ip != self._ip:
                        log.info("dns_pin: %s → %s (via %s)", self.host, ip, srv)
                    self._ip, self._ts = ip, now
                return ip
        with self._lock:
            return self._ip          # stale is better than nothing

    def pin(self, session) -> None:
        """Pin host→IP on this thread's curl handle. No-op if we can't resolve."""
        ip = self.resolve()
        if not ip:
            return
        try:
            from curl_cffi.const import CurlOpt
            pinned = getattr(_tls, "pinned", None)
            if pinned is None:
                pinned = _tls.pinned = {}
            key = (self.host, self.port)
            prev = pinned.get(key)
            entries = [f"{self.host}:{self.port}:{ip}"]
            if prev and prev != ip:
                entries.insert(0, f"-{self.host}:{self.port}")   # drop stale entry
            session.curl.setopt(CurlOpt.RESOLVE, entries)
            pinned[key] = ip
        except Exception:
            pass                      # never let pinning break a request

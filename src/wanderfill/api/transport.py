"""HTTP plumbing for the two NomadMania surfaces.

There are two of them and they do not share an authentication convention:

  modern   POST nomadmania.com/webapi/<module>/<action>   token in an NMTOKEN header
  legacy   POST nomadmania.com/ajax/<path>/               token in a form field

Both are form-encoded POSTs. Both answer 200 on failure. Everything below is
stdlib so the bare client installs anywhere.
"""

from __future__ import annotations

import gzip
import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .errors import ApiError, AuthError, TransportError, UnknownWriteOutcome

WEBAPI = "https://nomadmania.com/webapi/"
AJAX = "https://nomadmania.com/ajax/"

USER_AGENT = "wanderfill/0.1 (+https://github.com/faeton/wanderfill)"
"""Identify ourselves honestly.

An operator who can see which traffic is this tool can rate-limit or block it
specifically. That is a better outcome for everyone than being indistinguishable
from a browser and getting a whole traffic shape banned.
"""


@dataclass
class RateLimiter:
    """Keep writes at human speed.

    This is not politeness theatre. A profile that gains six hundred visits in
    ninety seconds looks like nothing a person does through the UI, and the
    account that does it is the one that gets suspended.
    """

    min_interval: float = 0.15
    _last: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def wait(self) -> None:
        # Locked because bulk reads run on a small thread pool; without it two
        # threads read the same ``_last`` and both go at once.
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


@dataclass
class Traffic:
    """What the network has cost so far: requests, bytes on the wire, seconds.

    Nomads are routinely on hotel wifi and roaming data, so a read-only command
    says what it spent. ``verbose`` prints every request as it completes.
    """

    requests: int = 0
    wire_bytes: int = 0
    seconds: float = 0.0
    verbose: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, action: str, wire: int, took: float) -> None:
        with self._lock:
            self.requests += 1
            self.wire_bytes += wire
            self.seconds += took
        if self.verbose:
            print(f"  {action}  {wire / 1024:.1f} KB  {took:.1f}s", file=sys.stderr, flush=True)

    def summary(self) -> str:
        return (f"network: {self.requests} request{'s' * (self.requests != 1)}, "
                f"{self.wire_bytes / 1024:,.0f} KB on the wire")


def _inflate(raw: bytes, encoding: str | None = None) -> bytes:
    """Undo gzip by the magic bytes, never by the header.

    Both ways the header lies: the tile server sends gzip without saying so
    (see geo/tiles), and a hotel proxy can decompress a body and leave
    ``Content-Encoding: gzip`` on it. Trusting the header there makes a write
    the server applied fail on our side, and an unknown write outcome blocks
    the journal until a human reconciles it. ``encoding`` is accepted and
    deliberately ignored.
    """
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


@dataclass
class Transport:
    """Does the talking. Knows nothing about what any endpoint means."""

    token: str
    lang: str = "en"
    timeout: float = 40.0
    retries: int = 3
    limiter: RateLimiter = field(default_factory=RateLimiter)
    # Reads are not what gets an account suspended, and on a slow link the cost
    # is latency per request, not bandwidth: 393 sequential visit reads at the
    # write pace spent a minute asleep before the first byte of round-trip.
    read_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(0.03))
    traffic: Traffic = field(default_factory=Traffic)
    # Keep one connection per thread open for reads. Every fresh HTTPS request
    # pays a TCP and TLS handshake — several round trips and a few KB of
    # certificates — before the first byte of a 25-byte answer.
    keep_alive: bool = True
    _local: threading.local = field(default_factory=threading.local, repr=False)

    def __post_init__(self) -> None:
        if not self.token or len(self.token) < 8:
            raise AuthError("no usable token; set NM_TOKEN")

    # -- the read connection pool -------------------------------------------

    def _pooled(self) -> bool:
        # urllib honours HTTPS_PROXY and friends; http.client does not. With a
        # proxy configured, go through urllib rather than around the proxy.
        return self.keep_alive and not urllib.request.getproxies().get("https")

    def _drop(self, host: str) -> None:
        conns = self._local.__dict__.setdefault("conns", {})
        if host in conns:
            conns.pop(host).close()

    def _conn(self, host: str, *, fresh: bool = False) -> http.client.HTTPSConnection:
        conns = self._local.__dict__.setdefault("conns", {})
        if fresh:
            self._drop(host)
        if host not in conns:
            conns[host] = http.client.HTTPSConnection(host, timeout=self.timeout)
        return conns[host]

    def _read_pooled(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[bytes, str]:
        """A read over this thread's kept-alive connection. Never used for writes.

        A server may close an idle connection at any time, and the next request
        on it then fails before it is read. For a read that is harmless: try once
        more on a fresh connection. For a write it would be an unknown outcome,
        which is why writes never come here.
        """
        parts = urllib.parse.urlsplit(url)
        for fresh in (False, True):
            conn = self._conn(parts.netloc, fresh=fresh)
            reused = getattr(conn, "sock", None) is not None
            try:
                conn.request("POST", parts.path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (http.client.HTTPException, OSError):
                self._drop(parts.netloc)
                if reused and not fresh:
                    continue
                raise
            if resp.status >= 400:
                raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
            return raw, resp.getheader("Content-Encoding") or ""
        raise AssertionError("unreachable")

    # -- low level ---------------------------------------------------------

    def _post(
        self, url: str, fields: dict[str, Any], headers: dict[str, str], *, idempotent: bool
    ) -> bytes:
        """One form POST.

        ``idempotent`` decides whether a lost answer may be retried, and it is
        required rather than defaulted because getting it wrong is the whole
        problem. **A retried write is how duplicates are made.** If the server
        accepted an ``add-visit`` and the response was lost on the way back,
        sending it again produces a second visit and a second phantom trip, and
        nothing in the journal shows two writes — this package already has an
        incident where fifty duplicate visits were created a different way.

        So reads retry, and writes get exactly one attempt. A write that fails
        without an answer from the server raises :class:`UnknownWriteOutcome`,
        because "it did not happen" is not something we know.
        """
        body = urllib.parse.urlencode(
            {k: v for k, v in fields.items() if v is not None}
        ).encode()
        all_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            # The catalogue is 173 KB plain and 42 KB gzipped; the country
            # list 48 KB and 8 KB. urllib asks for neither unless told.
            "Accept-Encoding": "gzip",
            **headers,
        }
        req = urllib.request.Request(url, data=body, headers=all_headers)
        # Writes always open a fresh connection: see _read_pooled.
        pooled = idempotent and self._pooled()
        last: Exception | None = None
        attempts = (self.retries + 1) if idempotent else 1
        limiter = self.read_limiter if idempotent else self.limiter
        for attempt in range(attempts):
            limiter.wait()
            started = time.monotonic()
            label = url.removeprefix(WEBAPI).removeprefix(AJAX)
            try:
                if pooled:
                    raw, _ = self._read_pooled(url, body, all_headers)
                else:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = resp.read()
                self.traffic.add(label, len(raw), time.monotonic() - started)
                return _inflate(raw)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise AuthError(f"server rejected the token ({exc.code})") from exc
                last = exc
            except Exception as exc:
                last = exc
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
        if not idempotent:
            # An HTTPError means the server answered and refused. Anything else
            # — timeout, reset, DNS — means we never learned what it did.
            if isinstance(last, urllib.error.HTTPError):
                raise TransportError(f"{url} refused: {last}")
            raise UnknownWriteOutcome(
                f"{url} sent, no answer received ({last}). "
                "Whether the server applied it is unknown; do not retry blind. "
                "Read the affected record back before running anything again."
            )
        raise TransportError(f"{url} failed after {attempts} attempts: {last}")

    # -- the two surfaces --------------------------------------------------

    # Actions that change server state. Everything else is a read and may be
    # retried freely. Kept as a suffix match so a new `set-`/`update-` endpoint
    # is treated as a write by default rather than by omission.
    WRITE_HINTS = ("add-", "update", "new-", "delete", "set-", "toggle")

    @classmethod
    def _is_write(cls, action: str) -> bool:
        tail = action.rsplit("/", 1)[-1]
        return any(h in tail for h in cls.WRITE_HINTS)

    def webapi(self, action: str, **fields: Any) -> dict:
        """Call the modern API and unwrap its in-body error convention."""
        raw = self._post(
            WEBAPI + action,
            fields,
            {"NMTOKEN": self.token, "LANG": self.lang, "platform": "web"},
            idempotent=not self._is_write(action),
        )
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("result") == "ERROR":
            raise ApiError(action, data.get("result_description", "unknown"), data)
        return data

    def ajax_json(self, path: str, **fields: Any) -> Any:
        """Call the legacy surface, where the token travels in the body.

        The write/read split cannot be read off the path here — this surface
        puts its verb in an ``action`` field — so the body is consulted too.
        """
        write = self._is_write(path) or self._is_write(str(fields.get("action", "")))
        raw = self._post(
            AJAX + path, {**fields, "token": self.token}, {}, idempotent=not write
        )
        return json.loads(raw)

    def ajax_text(self, path: str, **fields: Any) -> str:
        """Same, for the endpoints that answer with a bare string such as "OK".

        Never retried. The only caller is ``my_series/toggle``, and a toggle
        replayed against a server that already applied it is the one request on
        this whole surface that can silently *undo* the write it repeats.
        """
        raw = self._post(AJAX + path, {**fields, "token": self.token}, {}, idempotent=False)
        return raw.decode("utf-8", "replace").strip()

    # -- keep the token out of tracebacks and logs -------------------------

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Transport(token=<redacted>, lang={self.lang!r})"

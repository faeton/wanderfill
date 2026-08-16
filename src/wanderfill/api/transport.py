"""HTTP plumbing for the two NomadMania surfaces.

There are two of them and they do not share an authentication convention:

  modern   POST nomadmania.com/webapi/<module>/<action>   token in an NMTOKEN header
  legacy   POST nomadmania.com/ajax/<path>/               token in a form field

Both are form-encoded POSTs. Both answer 200 on failure. Everything below is
stdlib so the bare client installs anywhere.
"""

from __future__ import annotations

import json
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

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


@dataclass
class Transport:
    """Does the talking. Knows nothing about what any endpoint means."""

    token: str
    lang: str = "en"
    timeout: float = 40.0
    retries: int = 3
    limiter: RateLimiter = field(default_factory=RateLimiter)

    def __post_init__(self) -> None:
        if not self.token or len(self.token) < 8:
            raise AuthError("no usable token; set NM_TOKEN")

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
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                **headers,
            },
        )
        last: Exception | None = None
        attempts = (self.retries + 1) if idempotent else 1
        for attempt in range(attempts):
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
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

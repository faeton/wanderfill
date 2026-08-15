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

from .errors import ApiError, AuthError, TransportError

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

    def _post(self, url: str, fields: dict[str, Any], headers: dict[str, str]) -> bytes:
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
        for attempt in range(self.retries + 1):
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
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        raise TransportError(f"{url} failed after {self.retries + 1} attempts: {last}")

    # -- the two surfaces --------------------------------------------------

    def webapi(self, action: str, **fields: Any) -> dict:
        """Call the modern API and unwrap its in-body error convention."""
        raw = self._post(
            WEBAPI + action,
            fields,
            {"NMTOKEN": self.token, "LANG": self.lang, "platform": "web"},
        )
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("result") == "ERROR":
            raise ApiError(action, data.get("result_description", "unknown"), data)
        return data

    def ajax_json(self, path: str, **fields: Any) -> Any:
        """Call the legacy surface, where the token travels in the body."""
        raw = self._post(AJAX + path, {**fields, "token": self.token}, {})
        return json.loads(raw)

    def ajax_text(self, path: str, **fields: Any) -> str:
        """Same, for the endpoints that answer with a bare string such as "OK"."""
        raw = self._post(AJAX + path, {**fields, "token": self.token}, {})
        return raw.decode("utf-8", "replace").strip()

    # -- keep the token out of tracebacks and logs -------------------------

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Transport(token=<redacted>, lang={self.lang!r})"

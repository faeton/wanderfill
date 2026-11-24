"""Exceptions raised by the NomadMania client."""

from __future__ import annotations


class WanderfillError(Exception):
    """Base class for everything this package raises."""


class AuthError(WanderfillError):
    """The token is missing, malformed, or no longer accepted."""


class ApiError(WanderfillError):
    """The server answered, and the answer was a failure.

    NomadMania signals failure in the body rather than the status code:
    ``{"result": "ERROR", "result_description": "Missing params: regions."}``
    with HTTP 200. So this is raised from the body, not from the response code.
    """

    def __init__(self, action: str, description: str, payload: dict | None = None):
        self.action = action
        self.description = description
        self.payload = payload or {}
        super().__init__(f"{action}: {description}")


class VerificationFailed(ApiError):
    """The server said OK to a write, and the read-back does not prove it.

    Raised by the undo methods and ``sync_trip_header`` after the request went
    out, whether the read-back disagrees or could not be read at all, and by
    any strict read whose answer lacks the list it needs or holds an id that is
    not a number, before a write as well as after one. It is distinct from
    a plain :class:`ApiError` so that a caller keeping a journal can tell "the
    server refused, nothing changed" from "something was sent and the state is
    now unknown" — the second must stay unresolved until a human reads back.
    """


class UnreadableAnswer(WanderfillError, ValueError):
    """A read was answered with something that is not JSON.

    A captive portal's page, or an empty body. A ``ValueError`` as well, which is
    what it used to be. Not an :class:`ApiError`: the server refused nothing, so
    a journal entry is not closed on it. A strict read raises it on as
    :class:`VerificationFailed`, and a write as :class:`UnknownWriteOutcome`.
    """


class TransportError(WanderfillError):
    """A write the server refused with a 4xx, or a read that failed every attempt.

    Never a write whose outcome is in doubt: that is :class:`UnknownWriteOutcome`.
    """


class UnknownWriteOutcome(WanderfillError):
    """A write was sent and no usable answer came back.

    Nothing at all, a redirect, a 5xx or 408, a body that is not JSON, or a
    series toggle that said something other than ``OK``. Distinct from
    :class:`TransportError`, which on a write means the server answered and
    refused. Here we do not know whether the profile changed, and the difference
    matters: retrying a refused write is safe, retrying an unanswered one is how
    duplicate visits and phantom trips are made. The only correct response is to
    stop and read the affected record back.
    """


class PrecisionLoss(WanderfillError):
    """A write would have made an existing date vaguer than it already was.

    ``quickEnter/update-visit`` replaces the whole record, so posting a bare
    year over a full date deletes the month and day, and posting nothing deletes
    the year too. Neither is recoverable from the response, and neither looks
    like an error at the time. Raised before the request, not after.
    """


class DriftError(WanderfillError):
    """Live state changed between planning and applying, or may be changing.

    Applying a stale plan is how duplicates get created, so this stops the run
    and asks for a re-plan rather than guessing. A write still open in one of the
    account's journals, and another run holding the account's lock, stop it for
    the same reason, and so does a write that would put a second copy of a visit on the profile.
    """


class JournalCorrupt(WanderfillError):
    """A journal line cannot be read, so nobody knows which writes are open.

    A crash while appending leaves one. Skipping it would hide the write it
    stood for, so nothing is sent until a human has repaired the file; the tool
    never rewrites a journal.
    """


class AccountMismatch(WanderfillError):
    """The plan was generated for a different account than the one logged in.

    The single most damaging possible mistake is writing one person's travel
    history onto another person's profile. This exists so that cannot happen
    quietly.
    """


class PlanInvalid(WanderfillError, ValueError):
    """A plan file is malformed, or says something no write can honestly carry out.

    A ``ValueError`` as well, so a caller written before plans were validated as
    a whole, and which caught that, still catches this.
    """

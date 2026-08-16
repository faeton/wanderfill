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


class TransportError(WanderfillError):
    """The request never got an answer — timeout, DNS, connection reset."""


class UnknownWriteOutcome(WanderfillError):
    """A write was sent and no answer came back.

    Distinct from :class:`TransportError`, which means the server answered and
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
    """Live state changed between planning and applying.

    Applying a stale plan is how duplicates get created, so this stops the run
    and asks for a re-plan rather than guessing.
    """


class AccountMismatch(WanderfillError):
    """The plan was generated for a different account than the one logged in.

    The single most damaging possible mistake is writing one person's travel
    history onto another person's profile. This exists so that cannot happen
    quietly.
    """

"""wanderfill — import your location history into NomadMania.

Unofficial and unaffiliated. See README.md for what that means in practice.
"""

from .api.client import QUALITY, NomadMania, Visit
from .api.errors import (
    AccountMismatch,
    ApiError,
    AuthError,
    DriftError,
    TransportError,
    WanderfillError,
)

__version__ = "0.1.0"
__all__ = [
    "QUALITY",
    "AccountMismatch",
    "ApiError",
    "AuthError",
    "DriftError",
    "NomadMania",
    "TransportError",
    "Visit",
    "WanderfillError",
    "__version__",
]

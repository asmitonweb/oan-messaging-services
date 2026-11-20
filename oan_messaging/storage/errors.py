"""Storage-level errors. Messages are fixed strings: they never contain submitted values,
SQL parameters, keys or recipients."""

from __future__ import annotations


class StorageError(Exception):
    """Base class for MSG-02 storage errors."""


class ExpiryRejected(StorageError):
    """A NEW submission failed the time-sensitive expiry rule (HTTP 422)."""

    def __init__(self, code: str) -> None:
        super().__init__("expires_at rejected")
        #: ``required_for_otp`` | ``not_in_future`` | ``beyond_ceiling``
        self.code = code


class BindingIntegrityError(StorageError):
    """A live idempotency binding is missing its payload fingerprint, or a stored row
    violates the contract. Never treated as a new request."""


class MessageExpired(StorageError):
    """``begin_attempt`` refused: the message reached ``expires_at`` before the call."""


class DispatchNotAuthorized(StorageError):
    """``begin_attempt`` refused: the caller is disabled, lost the channel or purpose
    permission, or no longer maps to the Telegram bot authorized at acceptance. The
    claim's lease is released; the message stays ``accepted`` and is not claimable until
    authorization is restored (or it expires)."""


class KeyRetirementRefused(StorageError):
    """A key version is current or still referenced by retained records."""

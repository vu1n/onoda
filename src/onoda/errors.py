"""Error taxonomy. Every trust-boundary failure raises a specific subclass."""

from __future__ import annotations


class OnodaError(Exception):
    """Base class for all harness errors."""


class CanonicalizationError(OnodaError):
    """A value cannot be canonically encoded (float, unsafe int, unknown type)."""


class IntegrityError(OnodaError):
    """Bytes do not match their declared digest, or a hash chain is broken."""


class MissingObjectError(OnodaError):
    """A referenced artifact or record is absent from the store."""

    def __init__(self, ref: str, message: str | None = None):
        self.ref = ref
        super().__init__(message or f"missing object {ref}")


class SchemaVersionError(OnodaError):
    """A record declares a schema version this reader does not support."""


class ReplayMismatch(OnodaError):
    """An offline replay lookup found no recording for the exact request."""

    code = "replay_mismatch"


class ImportRejected(OnodaError):
    """A bundle failed a safety or closure check during import."""


class VisibilityError(OnodaError):
    """A reference is outside the caller's permitted view."""


class BudgetExceeded(OnodaError):
    """A reservation would exceed a run, branch, or experiment limit."""


class ForkError(OnodaError):
    """A fork request violates fork semantics (non-quiescent, bad boundary...)."""


class CapabilityError(OnodaError):
    """An operation was requested that the record does not support."""


class SubmissionUncertain(OnodaError):
    """An external submission's outcome is unknown and must be reconciled."""

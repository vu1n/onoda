"""Canonical JSON (RFC 8785 via the `rfc8785` library) and SHA-256 digests.

Structured records are hashed over their RFC 8785 canonical bytes. Floats are
rejected outright: exact scientific values travel as decimal strings with
explicit units, so no record's identity depends on binary float formatting.
"""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785

from labrun.errors import CanonicalizationError

DIGEST_PREFIX = "sha256:"
_MAX_SAFE_INT = 2**53 - 1


def _check(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise CanonicalizationError(f"integer outside IEEE-754 safe range at {path}")
        return
    if isinstance(value, float):
        raise CanonicalizationError(f"float at {path}; encode exact values as decimal strings")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string key at {path}")
            _check(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    raise CanonicalizationError(f"unsupported type {type(value).__name__} at {path}")


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 canonical encoding of a JSON-compatible value (no floats)."""
    _check(value, "$")
    return rfc8785.dumps(value)


def sha256_digest(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest_of(value: Any) -> str:
    """Digest of a structured value's canonical bytes."""
    return sha256_digest(canonical_bytes(value))


def is_digest(ref: str) -> bool:
    if not isinstance(ref, str) or not ref.startswith(DIGEST_PREFIX):
        return False
    hexpart = ref[len(DIGEST_PREFIX) :]
    return len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)

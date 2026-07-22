"""Opaque values and errors shared by current journal components."""

from __future__ import annotations

import hashlib


class CurrentJournalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DispatchCapability:
    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        object.__setattr__(self, "_secret", secret)

    def __repr__(self) -> str:
        return "DispatchCapability(<opaque>)"


def capability_sha256(capability: object) -> str:
    if type(capability) is not DispatchCapability:
        raise CurrentJournalError("DISPATCH_CAPABILITY_INVALID")
    secret = capability._secret
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise CurrentJournalError("DISPATCH_CAPABILITY_INVALID")
    return hashlib.sha256(secret).hexdigest()


def translate_contract_error(exc: Exception) -> None:
    if exc.__class__.__name__ == "ContractValidationError":
        raise CurrentJournalError("CURRENT_DOCUMENT_INVALID") from exc


__all__ = [
    "CurrentJournalError",
    "DispatchCapability",
    "capability_sha256",
    "translate_contract_error",
]

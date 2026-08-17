from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from types import MappingProxyType
from typing import Any

from robotics_runtime_contracts import (
    ArtifactReceiptValidationError,
    validate_artifact_receipt,
)

from robotics_acceptance_harness.documents import LoadedDocument, load_document


class ReceiptValidationError(ValueError):
    """Raised when an artifact receipt lacks a verified provenance chain."""

    def __init__(self, json_path: str, message: str) -> None:
        self.json_path = json_path
        self.validation_message = message
        super().__init__(f"{json_path}: {message}")


@dataclass(frozen=True, slots=True)
class VerifiedReceipt:
    receipt: LoadedDocument
    verification: LoadedDocument


@dataclass(frozen=True, slots=True)
class VerifiedReceiptSet:
    by_digest: Mapping[str, VerifiedReceipt]

    def verify_artifact(
        self,
        receipt_sha256: str,
        expected: Mapping[str, Any],
        *,
        run_id: str | None = None,
    ) -> VerifiedReceipt:
        verified = self.by_digest.get(receipt_sha256)
        if verified is None:
            raise ReceiptValidationError("$.receipt_sha256", "verified receipt was not supplied")
        artifact = verified.receipt.data["artifact"]
        observed = {field: artifact.get(field) for field in expected}
        if observed != dict(expected):
            raise ReceiptValidationError(
                "$.artifact",
                f"receipt describes different bytes: expected {dict(expected)!r}",
            )
        if run_id is not None and verified.receipt.data.get("run_id") != run_id:
            raise ReceiptValidationError("$.run_id", "receipt belongs to another run")
        return verified


def _raw_digests(paths: Sequence[str | Path]) -> Mapping[str, Path]:
    artifacts: dict[str, Path] = {}
    for value in paths:
        path = Path(value).expanduser().resolve()
        with path.open("rb") as stream:
            digest = file_digest(stream, "sha256").hexdigest()
        if digest in artifacts:
            raise ReceiptValidationError("$.dependencies", "duplicate dependency bytes")
        artifacts[digest] = path
    return MappingProxyType(artifacts)


def _documents(
    paths: Sequence[str | Path],
    role: str,
) -> Mapping[str, LoadedDocument]:
    documents: dict[str, LoadedDocument] = {}
    for path in paths:
        document = load_document(path, expected_role=role)
        if document.sha256 in documents:
            raise ReceiptValidationError(f"$.{role}", "duplicate document bytes")
        documents[document.sha256] = document
    return MappingProxyType(documents)


def load_verified_receipts(
    *,
    receipt_paths: Sequence[str | Path] = (),
    verification_paths: Sequence[str | Path] = (),
    dependency_paths: Sequence[str | Path] = (),
) -> VerifiedReceiptSet:
    """Load receipts and bind them to externally verified provenance records."""

    receipts = _documents(receipt_paths, "artifact_receipt")
    verifications = _documents(verification_paths, "artifact_verification")
    dependencies = _raw_digests(dependency_paths)
    used_verifications: set[str] = set()
    used_dependencies: set[str] = set()
    verified: dict[str, VerifiedReceipt] = {}
    for receipt_sha256, receipt in receipts.items():
        verification_sha256 = str(receipt.data["verification_sha256"])
        verification = verifications.get(verification_sha256)
        if verification is None:
            raise ReceiptValidationError(
                "$.verification_sha256",
                "receipt verification document was not supplied",
            )
        used_verifications.add(verification_sha256)
        try:
            required_dependencies = validate_artifact_receipt(
                receipt.data,
                verification.data,
                dependencies,
            )
        except ArtifactReceiptValidationError as error:
            raise ReceiptValidationError("$.verification_sha256", str(error)) from error
        used_dependencies.update(required_dependencies)
        verified[receipt_sha256] = VerifiedReceipt(receipt, verification)

    if used_verifications != set(verifications):
        raise ReceiptValidationError("$.verifications", "unreferenced verification document")
    if used_dependencies != set(dependencies):
        raise ReceiptValidationError("$.dependencies", "unreferenced provenance dependency")
    return VerifiedReceiptSet(MappingProxyType(verified))


__all__ = [
    "ReceiptValidationError",
    "VerifiedReceipt",
    "VerifiedReceiptSet",
    "load_verified_receipts",
]

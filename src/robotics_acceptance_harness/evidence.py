from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import file_digest
from os import name as os_name
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit

from robotics_acceptance_harness.documents import (
    BundleValidationError,
    LoadedDocument,
    load_document,
)
from robotics_acceptance_harness.receipts import (
    ReceiptValidationError,
    VerifiedReceiptSet,
    load_verified_receipts,
)


class EvidenceValidationError(ValueError):
    """Raised when finalized evidence cannot be independently verified."""

    def __init__(self, json_path: str, message: str) -> None:
        self.json_path = json_path
        self.validation_message = message
        super().__init__(f"{json_path}: {message}")


@dataclass(frozen=True, slots=True)
class VerifiedEvidence:
    index: LoadedDocument
    links: tuple[Mapping[str, Any], ...]
    local_files: Mapping[Path, Mapping[str, Any]]
    recording_summaries: tuple[LoadedDocument, ...] = ()
    receipts: tuple[LoadedDocument, ...] = ()


def _local_path(value: str) -> Path:
    decoded = unquote(value)
    if os_name == "nt" and decoded.startswith("/") and decoded[2:3] == ":":
        decoded = decoded[1:]
    return Path(decoded)


def _local_link(artifact: Mapping[str, Any], index: int) -> tuple[Path, Mapping[str, Any]]:
    path = _local_path(str(artifact["local_path"]))
    json_path = f"$.artifacts[{index}]"
    uri = urlsplit(str(artifact["uri"]))
    if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
        raise EvidenceValidationError(
            f"{json_path}.uri",
            "local evidence requires a local file URI",
        )
    uri_path = _local_path(uri.path)
    if uri_path.resolve() != path.resolve():
        raise EvidenceValidationError(
            f"{json_path}.local_path",
            f"does not identify file URI {artifact['uri']}",
        )
    if not path.is_file():
        raise EvidenceValidationError(f"{json_path}.local_path", f"file does not exist: {path}")
    observed_size = path.stat().st_size
    if observed_size != artifact["size_bytes"]:
        raise EvidenceValidationError(
            f"{json_path}.size_bytes",
            f"expected {artifact['size_bytes']}; observed {observed_size}",
        )
    with path.open("rb") as stream:
        observed_digest = file_digest(stream, "sha256").hexdigest()
    if observed_digest != artifact["sha256"]:
        raise EvidenceValidationError(
            f"{json_path}.sha256",
            f"expected {artifact['sha256']}; observed {observed_digest}",
        )
    return path.resolve(), _result_link(artifact)


def _remote_link(
    artifact: Mapping[str, Any],
    index: int,
    receipts: VerifiedReceiptSet,
    run_id: str,
) -> Mapping[str, Any]:
    json_path = f"$.artifacts[{index}]"
    expected = {
        field: artifact[field]
        for field in ("uri", "sha256", "size_bytes", "media_type", "immutable_revision")
    }
    try:
        receipts.verify_artifact(
            str(artifact["receipt_sha256"]),
            expected,
            run_id=run_id,
        )
    except ValueError as error:
        raise EvidenceValidationError(json_path, str(error)) from error
    return _result_link(artifact)


def _result_link(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = (
        "artifact_id",
        "kind",
        "uri",
        "immutable_revision",
        "receipt_sha256",
        "media_type",
        "sha256",
        "size_bytes",
        "retention_class",
        "segment_index",
    )
    return MappingProxyType({field: artifact[field] for field in fields if field in artifact})


def _local_summary(
    artifact: Mapping[str, Any],
    index: int,
) -> LoadedDocument:
    reference = artifact["recording_summary"]
    json_path = f"$.artifacts[{index}].recording_summary"
    uri = urlsplit(str(reference["uri"]))
    if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
        raise EvidenceValidationError(
            f"{json_path}.uri",
            "acceptance verification requires a local recording summary",
        )
    path = _local_path(uri.path).resolve()
    if not path.is_file():
        raise EvidenceValidationError(f"{json_path}.uri", f"file does not exist: {path}")
    if path.stat().st_size != reference["size_bytes"]:
        raise EvidenceValidationError(f"{json_path}.size_bytes", "summary size differs")
    try:
        summary = load_document(path, expected_role="recording_summary")
    except BundleValidationError as error:
        raise EvidenceValidationError(error.json_path, error.validation_message) from error
    if summary.sha256 != reference["sha256"]:
        raise EvidenceValidationError(f"{json_path}.sha256", "summary digest differs")
    if summary.data["source_sha256"] != artifact["sha256"]:
        raise EvidenceValidationError(
            f"{json_path}.source_sha256",
            "summary does not identify its recording artifact",
        )
    return summary


def load_evidence_index(
    path: str | Path,
    *,
    expected_run_id: str | None = None,
    receipt_paths: Sequence[str | Path] = (),
    verification_paths: Sequence[str | Path] = (),
    receipt_dependency_paths: Sequence[str | Path] = (),
) -> VerifiedEvidence:
    """Validate a finalized index and verify every reusable evidence link."""

    try:
        document = load_document(
            path,
            expected_role="evidence_index",
        )
    except BundleValidationError as error:
        raise EvidenceValidationError(error.json_path, error.validation_message) from error
    if expected_run_id is not None and document.data["run_id"] != expected_run_id:
        raise EvidenceValidationError(
            "$.run_id",
            f"expected {expected_run_id!r}; received {document.data['run_id']!r}",
        )

    links: list[Mapping[str, Any]] = []
    local_files: dict[Path, Mapping[str, Any]] = {}
    summaries: list[LoadedDocument] = []
    try:
        receipts = load_verified_receipts(
            receipt_paths=receipt_paths,
            verification_paths=verification_paths,
            dependency_paths=receipt_dependency_paths,
        )
    except ReceiptValidationError as error:
        raise EvidenceValidationError(error.json_path, error.validation_message) from error
    used_receipts: set[str] = set()
    run_id = str(document.data["run_id"])
    for index, artifact in enumerate(document.data["artifacts"]):
        if artifact["storage_state"] == "local":
            local_path, link = _local_link(artifact, index)
            local_files[local_path] = link
            links.append(link)
        else:
            links.append(_remote_link(artifact, index, receipts, run_id))
            used_receipts.add(str(artifact["receipt_sha256"]))
        if artifact["kind"] == "recording":
            summaries.append(_local_summary(artifact, index))
    if used_receipts != set(receipts.by_digest):
        raise EvidenceValidationError("$.receipts", "unreferenced artifact receipt")
    return VerifiedEvidence(
        document,
        tuple(links),
        MappingProxyType(local_files),
        tuple(summaries),
        tuple(item.receipt for item in receipts.by_digest.values()),
    )

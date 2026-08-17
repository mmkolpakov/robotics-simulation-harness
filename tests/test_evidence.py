from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from robotics_acceptance_harness.evidence import EvidenceValidationError, load_evidence_index
from tests.support import evidence_index, local_evidence_artifact, write_verified_receipt

RUN_ID = "run-01234567-89ab-4def-8123-456789abcdef"


def _index(path: Path, *, digest: str | None = None, size: int | None = None) -> dict[str, object]:
    overrides = {
        key: value for key, value in (("sha256", digest), ("size_bytes", size)) if value is not None
    }
    return evidence_index(
        RUN_ID,
        [local_evidence_artifact(path, media_type="application/json", overrides=overrides)],
    )


def _write_index(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "evidence-index.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _retained_artifact(tmp_path: Path) -> tuple[dict[str, object], dict[str, Any]]:
    artifact = {
        "artifact_id": "run-observation",
        "kind": "observation",
        "uri": "s3://robotics-evidence/run.json",
        "media_type": "application/json",
        "sha256": "a" * 64,
        "size_bytes": 2048,
        "retention_class": "regression-30d",
        "storage_state": "retained",
        "immutable_revision": "version:3LgExampleVersion",
    }
    chain = write_verified_receipt(
        tmp_path,
        artifact,
        stem="run-observation",
        run_id=RUN_ID,
    )
    artifact["receipt_sha256"] = chain["receipt_sha256"]
    return artifact, chain


def test_verified_local_evidence_becomes_result_link(tmp_path: Path) -> None:
    artifact = tmp_path / "run.json"
    artifact.write_bytes(b"verified evidence")

    verified = load_evidence_index(
        _write_index(tmp_path, _index(artifact)),
        expected_run_id=RUN_ID,
    )

    assert verified.links[0]["uri"] == artifact.as_uri()
    assert artifact.resolve() in verified.local_files
    assert "local_path" not in verified.links[0]
    assert "storage_state" not in verified.links[0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("digest", "0" * 64, "sha256"),
        ("size", 1, "size_bytes"),
    ],
)
def test_tampered_local_evidence_is_rejected(
    tmp_path: Path,
    field: str,
    value: str | int,
    message: str,
) -> None:
    artifact = tmp_path / "run.json"
    artifact.write_bytes(b"verified evidence")
    options = {field: value}

    with pytest.raises(EvidenceValidationError, match=message):
        load_evidence_index(_write_index(tmp_path, _index(artifact, **options)))


def test_missing_local_evidence_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "run.json"
    artifact.write_bytes(b"verified evidence")
    index = _write_index(tmp_path, _index(artifact))
    artifact.unlink()

    with pytest.raises(EvidenceValidationError, match="does not exist"):
        load_evidence_index(index)


def test_retained_evidence_is_verified_from_an_immutable_receipt(tmp_path: Path) -> None:
    artifact, chain = _retained_artifact(tmp_path)
    document = evidence_index(
        RUN_ID,
        [artifact],
        upload_mode="closed_segments_during_run",
    )

    verified = load_evidence_index(
        _write_index(tmp_path, document),
        receipt_paths=[chain["receipt"]],
        verification_paths=[chain["verification"]],
        receipt_dependency_paths=chain["dependencies"],
    )

    assert verified.links[0]["immutable_revision"] == "version:3LgExampleVersion"
    assert verified.receipts[0].sha256 == artifact["receipt_sha256"]


def test_retained_evidence_without_a_receipt_is_rejected(tmp_path: Path) -> None:
    artifact, _receipt_path = _retained_artifact(tmp_path)
    document = evidence_index(
        RUN_ID,
        [artifact],
        upload_mode="closed_segments_during_run",
    )

    with pytest.raises(EvidenceValidationError, match="verified receipt was not supplied"):
        load_evidence_index(_write_index(tmp_path, document))


def test_remote_revision_cannot_be_relabelled_after_external_verification(
    tmp_path: Path,
) -> None:
    artifact, chain = _retained_artifact(tmp_path)
    artifact["immutable_revision"] = "version:forged"
    receipt_path = Path(chain["receipt"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact"]["immutable_revision"] = "version:forged"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    artifact["receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    document = evidence_index(
        RUN_ID,
        [artifact],
        upload_mode="closed_segments_during_run",
    )

    with pytest.raises(EvidenceValidationError, match="artifact descriptor"):
        load_evidence_index(
            _write_index(tmp_path, document),
            receipt_paths=[receipt_path],
            verification_paths=[chain["verification"]],
            receipt_dependency_paths=chain["dependencies"],
        )


def test_run_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "run.json"
    artifact.write_bytes(b"verified evidence")

    with pytest.raises(EvidenceValidationError, match="run_id"):
        load_evidence_index(
            _write_index(tmp_path, _index(artifact)),
            expected_run_id="run-01234567-89ab-4def-8123-456789abcdea",
        )

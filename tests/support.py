from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from os import name as os_name
from pathlib import Path
from typing import Any

import yaml

EXTENSION_SCHEMA_URI = "https://schemas.example.org/sorting-item.v1.schema.json"


def write_extended_scenario(
    directory: Path,
    base_scenario: Path,
) -> tuple[Path, Path, str]:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": EXTENSION_SCHEMA_URI,
        "type": "object",
        "additionalProperties": False,
        "required": ["item_id"],
        "properties": {"item_id": {"type": "string", "minLength": 1}},
    }
    raw_schema = json.dumps(schema, separators=(",", ":"), sort_keys=True).encode()
    schema_path = directory / "sorting-extension.schema.json"
    schema_path.write_bytes(raw_schema)

    scenario = yaml.safe_load(base_scenario.read_text(encoding="utf-8"))
    scenario["extension_schemas"] = [
        {
            "namespace": "org.example.sorting",
            "schema_uri": EXTENSION_SCHEMA_URI,
            "sha256": sha256(raw_schema).hexdigest(),
        }
    ]
    scenario["extensions"] = {"org.example.sorting": {"item_id": "parcel-42"}}
    scenario_path = directory / "scenario-with-extension.yaml"
    scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return scenario_path, schema_path, EXTENSION_SCHEMA_URI


class FakeTime:
    def __init__(self) -> None:
        self.value_ns = 0

    def now_ns(self) -> int:
        return self.value_ns

    def sleep(self, seconds: float) -> None:
        self.value_ns += int(seconds * 1_000_000_000)


def acceptance_run(
    *,
    run_id: str,
    scenario_id: str,
    scenario_sha256: str,
    time_kind: str,
    source_id: str,
    domains: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": "acceptance-run.v1",
        "run_id": run_id,
        "created_at": "2026-07-26T12:00:00Z",
        "scenario_id": scenario_id,
        "scenario_sha256": scenario_sha256,
        "time_authority": {"kind": time_kind, "source_id": source_id},
        "domains": [dict(domain) for domain in domains],
    }


def local_evidence_artifact(
    path: Path,
    *,
    media_type: str = "application/json",
    artifact_index: int = 0,
    retention_class: str = "pull-request-7d",
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = path.resolve()
    local_path = resolved.as_posix()
    if os_name == "nt":
        local_path = f"/{local_path}"
    artifact = {
        "artifact_id": f"artifact-{artifact_index}",
        "kind": "observation",
        "uri": resolved.as_uri(),
        "local_path": local_path,
        "media_type": media_type,
        "sha256": sha256(resolved.read_bytes()).hexdigest(),
        "size_bytes": resolved.stat().st_size,
        "retention_class": retention_class,
        "segment_index": artifact_index,
        "storage_state": "local",
    }
    artifact.update(overrides or {})
    return artifact


def local_recording_artifact(
    path: Path,
    *,
    topics: Mapping[str, str],
    duration_ns: int = 2_000_000_000,
    retention_class: str = "pull-request-7d",
) -> dict[str, Any]:
    path.write_bytes(b"mcap")
    artifact = local_evidence_artifact(
        path,
        media_type="application/mcap",
        retention_class=retention_class,
        overrides={"kind": "recording"},
    )
    summary = path.with_suffix(".recording-summary.json")
    summary.write_text(
        json.dumps(
            {
                "schema_version": "recording-summary.v1",
                "source_sha256": artifact["sha256"],
                "statistics": {
                    "message_count": len(topics),
                    "schema_count": len(topics),
                    "channel_count": len(topics),
                    "attachment_count": 0,
                    "metadata_count": 0,
                    "chunk_count": 1,
                    "message_start_time_ns": 1,
                    "message_end_time_ns": duration_ns + 1,
                },
                "compressions": ["zstd"],
                "channels": [
                    {
                        "topic": topic,
                        "message_encoding": "cdr",
                        "schema_name": schema,
                        "message_count": 1,
                    }
                    for topic, schema in topics.items()
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    artifact["recording_summary"] = {
        "uri": summary.resolve().as_uri(),
        "sha256": sha256(summary.read_bytes()).hexdigest(),
        "size_bytes": summary.stat().st_size,
    }
    return artifact


def evidence_index(
    run_id: str,
    artifacts: Sequence[Mapping[str, Any]],
    *,
    recording_mode: str = "on_failure",
    upload_mode: str = "local_only",
) -> dict[str, Any]:
    retention_classes = {str(item["retention_class"]) for item in artifacts}
    if len(retention_classes) != 1:
        raise ValueError("evidence artifacts require one retention class")
    return {
        "schema_version": "evidence-index.v1",
        "run_id": run_id,
        "generated_at": "2026-07-26T12:00:02Z",
        "finalized": True,
        "policy_observation": {
            "recording_mode": recording_mode,
            "compression": "zstd",
            "retention_class": retention_classes.pop(),
            "upload_mode": upload_mode,
            "remote_sink_used": upload_mode != "local_only",
            "spool_peak_size_bytes": sum(int(item["size_bytes"]) for item in artifacts),
            "upload_lag_max_sec": 0,
        },
        "artifacts": [dict(item) for item in artifacts],
    }


def write_evidence_index(
    path: Path,
    *,
    run_id: str,
    artifacts: Sequence[Mapping[str, Any]],
    recording_mode: str = "on_failure",
    upload_mode: str = "local_only",
) -> Path:
    path.write_text(
        yaml.safe_dump(
            evidence_index(
                run_id,
                artifacts,
                recording_mode=recording_mode,
                upload_mode=upload_mode,
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def write_verified_receipt(
    directory: Path,
    artifact: Mapping[str, Any],
    *,
    stem: str = "artifact",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create a complete fixture receipt and external verification chain."""

    def write_json(path: Path, value: Mapping[str, Any]) -> str:
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        return sha256(path.read_bytes()).hexdigest()

    statement_path = directory / f"{stem}-statement.json"
    statement_sha256 = write_json(
        statement_path,
        {"artifact_sha256": artifact["sha256"], "predicate_type": "fixture-build"},
    )
    trust_path = directory / f"{stem}-trust.json"
    trust_sha256 = write_json(trust_path, {"issuer": "fixture", "subject": stem})
    verification_evidence_path = directory / f"{stem}-verification.bundle"
    verification_evidence_path.write_bytes(b"fixture verification bundle\n")
    verification_evidence_sha256 = sha256(verification_evidence_path.read_bytes()).hexdigest()
    producer = {
        "identity": "https://github.com/example/fixture/.github/workflows/release.yml",
        "implementation": "fixture-producer",
    }
    verification = {
        "schema_version": "artifact-verification.v1",
        "verification_id": f"{stem}-verification",
        "statement_sha256": statement_sha256,
        "artifact": {
            field: artifact[field]
            for field in ("uri", "sha256", "size_bytes", "media_type", "immutable_revision")
        },
        "producer_identity": producer["identity"],
        "producer_implementation": producer["implementation"],
        "trust_policy_sha256": trust_sha256,
        "verification_evidence_sha256": verification_evidence_sha256,
        "verifier": {
            "identity": "fixture-verifier",
            "implementation": "cosign",
            "version": "3.0.5",
        },
        "verified_at": "2026-07-26T12:00:01Z",
        "status": "passed",
    }
    verification_path = directory / f"{stem}-verification.json"
    verification_sha256 = write_json(verification_path, verification)
    receipt = {
        "schema_version": "artifact-receipt.v1",
        "receipt_id": f"{stem}-receipt",
        "artifact": {
            field: artifact[field]
            for field in ("uri", "sha256", "size_bytes", "media_type", "immutable_revision")
        },
        "producer": producer,
        "created_at": "2026-07-26T12:00:02Z",
        "statement_sha256": statement_sha256,
        "verification_sha256": verification_sha256,
        **({"run_id": run_id} if run_id is not None else {}),
    }
    receipt_path = directory / f"{stem}-receipt.json"
    receipt_sha256 = write_json(receipt_path, receipt)
    return {
        "receipt": receipt_path,
        "receipt_sha256": receipt_sha256,
        "verification": verification_path,
        "dependencies": (
            statement_path,
            trust_path,
            verification_evidence_path,
        ),
    }

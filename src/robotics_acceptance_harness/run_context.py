from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from robotics_acceptance_harness.documents import (
    BundleValidationError,
    LoadedDocument,
    load_document,
)
from robotics_acceptance_harness.result import format_utc_datetime, write_contract_json


def create_run_context(
    scenario_path: str | Path,
    output_path: str | Path,
    *,
    domains: Mapping[str, str],
    time_authority: str,
    time_source: str,
    run_id: str | None = None,
    now: datetime | None = None,
    extension_schemas: Mapping[str, bytes | str] | None = None,
) -> str:
    """Create and validate the immutable context for one acceptance run."""

    scenario = load_document(
        scenario_path,
        expected_role="acceptance_scenario",
        extension_schemas=extension_schemas,
    )
    resolved_run_id = run_id or f"run-{uuid4()}"
    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    document = {
        "schema_version": "acceptance-run.v1",
        "run_id": resolved_run_id,
        "created_at": format_utc_datetime(created_at),
        "scenario_id": scenario.data["scenario_id"],
        "scenario_sha256": scenario.sha256,
        "time_authority": {
            "kind": time_authority,
            "source_id": time_source,
        },
        "domains": [
            {"domain_id": domain_id, "role": role} for domain_id, role in sorted(domains.items())
        ],
    }
    write_contract_json(document, output_path)
    return resolved_run_id


def load_run_context(
    path: str | Path,
    *,
    run_id: str,
    domain_id: str,
    scenario_id: str,
    scenario_sha256: str,
) -> LoadedDocument:
    """Load an immutable run context and bind it to one domain execution."""

    context = load_document(path, expected_role="acceptance_run")
    if context.data["run_id"] != run_id:
        raise BundleValidationError("$.run_id", "run context does not match --run-id")
    if context.data["scenario_id"] != scenario_id:
        raise BundleValidationError("$.scenario_id", "run context does not match the scenario")
    if context.data["scenario_sha256"] != scenario_sha256:
        raise BundleValidationError(
            "$.scenario_sha256",
            "run context does not match the scenario digest",
        )
    domains = {item["domain_id"] for item in context.data["domains"]}
    if domain_id not in domains:
        raise BundleValidationError(
            "$.domains",
            f"domain {domain_id!r} is not registered in the run context",
        )
    return context

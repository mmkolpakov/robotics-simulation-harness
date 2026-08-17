from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from robotics_acceptance_harness.documents import BundleValidationError, load_document
from robotics_acceptance_harness.result import format_utc_datetime, write_contract_json


def aggregate_campaign(
    *,
    scenario_path: str | Path,
    run_context_paths: Sequence[str | Path],
    aggregate_paths: Sequence[str | Path],
    output_path: str | Path,
    minimum_passed_runs: int,
    maximum_failed_runs: int = 0,
    maximum_incomplete_runs: int = 0,
    maximum_error_runs: int = 0,
    parameters: Mapping[str, Mapping[str, Any]] | None = None,
    campaign_id: str | None = None,
    generated_at: datetime | None = None,
    extension_schemas: Mapping[str, bytes | str] | None = None,
) -> Path:
    """Build a digest-linked campaign verdict from existing run aggregates."""

    if not aggregate_paths:
        raise BundleValidationError("$.runs", "at least one aggregate is required")
    scenario = load_document(
        scenario_path,
        expected_role="acceptance_scenario",
        extension_schemas=extension_schemas,
    )
    contexts = [load_document(path, expected_role="acceptance_run") for path in run_context_paths]
    context_by_run = {str(item.data["run_id"]): item for item in contexts}
    if len(context_by_run) != len(contexts):
        raise BundleValidationError("$.runs", "run context IDs must be unique")
    aggregates = [
        load_document(path, expected_role="acceptance_aggregate") for path in aggregate_paths
    ]
    run_ids = [str(item.data["run_id"]) for item in aggregates]
    if len(run_ids) != len(set(run_ids)):
        raise BundleValidationError("$.runs", "aggregate run_id values must be unique")
    if set(context_by_run) != set(run_ids):
        raise BundleValidationError(
            "$.runs",
            "run contexts must cover every aggregate run exactly once",
        )
    parameter_map = parameters or {}
    unknown = set(parameter_map) - set(run_ids)
    if unknown:
        raise BundleValidationError("$.runs.parameters", f"unknown run IDs: {sorted(unknown)}")

    runs: list[dict[str, Any]] = []
    counts = {"passed": 0, "failed": 0, "incomplete": 0, "error": 0}
    for aggregate in aggregates:
        run_id = str(aggregate.data["run_id"])
        context = context_by_run[run_id]
        if (
            context.data["scenario_id"] != scenario.data["scenario_id"]
            or context.data["scenario_sha256"] != scenario.sha256
        ):
            raise BundleValidationError(
                "$.scenario_sha256",
                f"run {run_id} identifies another scenario",
            )
        if aggregate.data["acceptance_run_sha256"] != context.sha256:
            raise BundleValidationError(
                "$.runs.acceptance_run_sha256",
                f"aggregate {run_id} identifies another run context",
            )
        cross_status = str(aggregate.data["cross_domain_e2e"]["status"])
        status = (
            str(aggregate.data["per_domain_aggregate"])
            if cross_status == "unevaluated"
            else cross_status
        )
        if status not in counts:
            status = "incomplete"
        counts[status] += 1
        runs.append(
            {
                "run_id": run_id,
                "acceptance_run_sha256": context.sha256,
                "aggregate_sha256": aggregate.sha256,
                "parameters": dict(parameter_map.get(run_id, {})),
                "status": status,
            }
        )

    policy_passed = (
        counts["passed"] >= minimum_passed_runs
        and counts["failed"] <= maximum_failed_runs
        and counts["incomplete"] <= maximum_incomplete_runs
        and counts["error"] <= maximum_error_runs
    )
    if policy_passed:
        verdict = "passed"
    else:
        priority = ("error", "failed", "incomplete")
        verdict = next((status for status in priority if counts[status]), "failed")
    document = {
        "schema_version": "campaign-summary.v1",
        "campaign_id": campaign_id or f"campaign-{uuid4()}",
        "scenario_id": scenario.data["scenario_id"],
        "scenario_sha256": scenario.sha256,
        "generated_at": format_utc_datetime(generated_at or datetime.now(UTC)),
        "runs": sorted(runs, key=lambda item: item["run_id"]),
        "acceptance": {
            "minimum_passed_runs": minimum_passed_runs,
            "maximum_failed_runs": maximum_failed_runs,
            "maximum_incomplete_runs": maximum_incomplete_runs,
            "maximum_error_runs": maximum_error_runs,
        },
        "verdict": {
            "status": verdict,
            "total_runs": len(runs),
            "passed_runs": counts["passed"],
            "failed_runs": counts["failed"],
            "incomplete_runs": counts["incomplete"],
            "error_runs": counts["error"],
        },
    }
    return write_contract_json(document, output_path)


__all__ = ["aggregate_campaign"]

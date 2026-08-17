from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from tempfile import mkstemp
from typing import Any

from junitparser import Error, Failure, JUnitXml, Skipped, TestCase, TestSuite
from robotics_runtime_contracts import validate_document, worst_status

from robotics_acceptance_harness.documents import DocumentBundle
from robotics_acceptance_harness.evidence import VerifiedEvidence
from robotics_acceptance_harness.forbidden_graph import ForbiddenGraphObservation
from robotics_acceptance_harness.hardware_timing import HardwareTimingObservation
from robotics_acceptance_harness.metrics import AssertionEvaluation
from robotics_acceptance_harness.readiness import GraphSnapshot, ReadinessResult
from robotics_acceptance_harness.time_authority import TimeAuthorityObservation
from robotics_acceptance_harness.timing import TimingObservation


def format_utc_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _workload_result(runtime: Mapping[str, Any]) -> dict[str, Any]:
    workload = runtime["workload"]
    if workload["kind"] == "none":
        return {"kind": "none"}
    return {
        "kind": "inference",
        "runtime_family": workload["inference"]["runtime_family"],
        "actual_provider": workload["inference"]["actual_provider"],
        "model_format": workload["model"]["format"],
        "fallback_count": workload["inference"]["fallback_count"],
    }


def _runtime_observation(runtime: Mapping[str, Any]) -> dict[str, Any]:
    data_plane = runtime["data_plane"]
    security = runtime["security"]
    return {
        "execution_subject_digest": runtime["execution_subject"]["digest"],
        "ros_domain_id": runtime["ros"]["domain_id"],
        "rmw_implementation": runtime["ros"]["rmw_implementation"],
        "rmw_version": runtime["ros"]["rmw_version"],
        **(
            {"middleware_configuration_sha256": data_plane["middleware_configuration_sha256"]}
            if "middleware_configuration_sha256" in data_plane
            else {}
        ),
        "security_enabled": security["profile"] != "none",
        "security_strategy": security["strategy"],
    }


def _observed_graph(readiness: ReadinessResult) -> dict[str, Any]:
    snapshot = readiness.snapshot
    return {
        "stable_for_sec": readiness.stable_for_sec,
        "topics": [
            {
                "name": name,
                "type": observation.types[0],
                "publishers": observation.publishers,
                "subscribers": observation.subscribers,
                "first_message_at_ns": observation.first_message_at_ns,
            }
            for name, observation in sorted(snapshot.topics.items())
            if observation.first_message_at_ns is not None and observation.types
        ],
        "services": [
            {
                "name": name,
                "type": observation.types[0],
                "server_nodes": observation.server_nodes,
                "client_nodes": observation.client_nodes,
            }
            for name, observation in sorted(snapshot.services.items())
            if (observation.server_nodes > 0 or observation.client_nodes > 0) and observation.types
        ],
        "actions": [
            {
                "name": name,
                "type": observation.types[0],
                "server_nodes": observation.server_nodes,
                "client_nodes": observation.client_nodes,
            }
            for name, observation in sorted(snapshot.actions.items())
            if (observation.server_nodes > 0 or observation.client_nodes > 0) and observation.types
        ],
    }


def _lifecycle_states(snapshot: GraphSnapshot) -> list[dict[str, Any]]:
    return [
        {
            "node": name,
            "state": observation.state,
            "observed_at_ns": observation.observed_at_ns,
        }
        for name, observation in sorted(snapshot.lifecycle_nodes.items())
    ]


def _forbidden_graph_result(observation: ForbiddenGraphObservation) -> dict[str, Any]:
    return {
        "passed": observation.passed,
        "checked_topics": list(observation.checked_topics),
        "checked_services": list(observation.checked_services),
        "checked_actions": list(observation.checked_actions),
        "violations": [
            {"kind": violation.kind, "name": violation.name} for violation in observation.violations
        ],
    }


def _authorization_result(bundle: DocumentBundle) -> dict[str, Any]:
    execution = bundle.scenario.data["execution"]
    if execution["target_environment"] == "simulation":
        return {"mode": "none"}
    if bundle.permit is None or bundle.verification is None:
        raise ValueError("physical acceptance result requires verified authorization")
    return {
        "mode": "verified_execution_permit",
        "permit_sha256": bundle.permit.sha256,
        "execution_verification_sha256": bundle.verification.sha256,
        "trust_policy_sha256": bundle.verification.data["trust_policy_sha256"],
        "target": dict(bundle.verification.data["target"]),
    }


def _hardware_clock_result(
    observation: HardwareTimingObservation,
    evidence_sha256: str,
) -> dict[str, Any]:
    return {
        "sync_protocol": observation.sync_protocol,
        "source": observation.source,
        "measured_at": format_utc_datetime(observation.measured_at),
        "sample_count": observation.sample_count,
        "offset_ms": observation.offset_ms,
        "jitter_ms": observation.jitter_ms,
        "drift_ppm": observation.drift_ppm,
        "max_sample_age_ms": observation.max_sample_age_ms,
        "within_policy": observation.within_policy,
        "evidence_sha256": evidence_sha256,
    }


def build_acceptance_result(
    *,
    result_id: str,
    run_id: str,
    domain_id: str,
    bundle: DocumentBundle,
    readiness: ReadinessResult,
    timing: TimingObservation,
    time_authority: TimeAuthorityObservation,
    time_authority_evidence_sha256: str | None,
    assertions: Sequence[AssertionEvaluation],
    unevaluated: Sequence[str],
    started_at: datetime,
    finished_at: datetime,
    monotonic_duration_sec: float,
    shutdown: Mapping[str, bool],
    evidence_index: VerifiedEvidence,
    forbidden_graph: ForbiddenGraphObservation,
    hardware_timing: HardwareTimingObservation | None = None,
    hardware_timing_evidence_sha256: str | None = None,
    evaluation_mode: str = "live",
) -> dict[str, Any]:
    """Build and validate the canonical per-domain acceptance result."""

    if evidence_index.index.data["run_id"] != run_id:
        raise ValueError("evidence index run_id must equal result run_id")
    physical = bundle.scenario.data["execution"]["target_environment"] in {
        "hil",
        "real_robot",
    }
    if physical and (hardware_timing is None or hardware_timing_evidence_sha256 is None):
        raise ValueError("physical acceptance result requires hardware timing evidence")
    if not physical and (
        hardware_timing is not None or hardware_timing_evidence_sha256 is not None
    ):
        raise ValueError("simulation acceptance result does not accept hardware timing")

    effective_unevaluated = set(unevaluated)
    effective_unevaluated.update(
        f"$.assertions.{evaluation.assertion_id}"
        for evaluation in assertions
        if evaluation.status == "skipped"
    )
    if assertions:
        result_status = worst_status({evaluation.status for evaluation in assertions})
    else:
        result_status = "incomplete"
        effective_unevaluated.add("$.assertions")
    if result_status == "skipped" or (
        result_status == "passed"
        and any(evaluation.status == "skipped" for evaluation in assertions)
    ):
        result_status = "incomplete"
    if result_status == "passed" and (
        not forbidden_graph.passed
        or (hardware_timing is not None and not hardware_timing.within_policy)
    ):
        result_status = "failed"
    evaluated = "$.time_authority_observation" not in effective_unevaluated
    if result_status != "error" and evaluated and not time_authority.within_policy:
        result_status = "failed"
    elif result_status == "passed" and effective_unevaluated:
        result_status = "incomplete"

    result: dict[str, Any] = {
        "schema_version": "acceptance-result.v1",
        "result_id": result_id,
        "run_id": run_id,
        "scenario_id": bundle.scenario.data["scenario_id"],
        "domain_id": domain_id,
        "verdict_scope": "domain",
        "evaluation_mode": evaluation_mode,
        "unevaluated": sorted(effective_unevaluated),
        "scenario_sha256": bundle.scenario.sha256,
        "runtime_manifest_sha256": bundle.runtime.sha256,
        "started_at": format_utc_datetime(started_at),
        "finished_at": format_utc_datetime(finished_at),
        "monotonic_duration_sec": monotonic_duration_sec,
        "status": result_status,
        "assertion_results": [
            {
                "assertion_id": evaluation.assertion_id,
                "source": evaluation.source,
                "status": evaluation.status,
                "observed_value": evaluation.observed_value,
                "unit": evaluation.unit,
                **({"message": evaluation.message} if evaluation.message else {}),
                **({"namespace": evaluation.namespace} if evaluation.namespace else {}),
                **(
                    {"evidence_sha256": list(evaluation.evidence_sha256)}
                    if evaluation.evidence_sha256
                    else {}
                ),
            }
            for evaluation in assertions
        ],
        "observed_ros_graph": _observed_graph(readiness),
        "forbidden_graph_observation": _forbidden_graph_result(forbidden_graph),
        "execution": dict(bundle.runtime.data["execution"]),
        "runtime_observation": _runtime_observation(bundle.runtime.data),
        "evaluators": [dict(item) for item in bundle.scenario.data["evaluator_requirements"]],
        "workload": _workload_result(bundle.runtime.data),
        "authorization": _authorization_result(bundle),
        "lifecycle_states": _lifecycle_states(readiness.snapshot),
        "clock_observation": {
            "monotonic": timing.monotonic,
            "offset_ms": timing.offset_ms,
            "drift_ppm": timing.drift_ppm,
            "real_time_factor": timing.real_time_factor,
            "deadline_miss_ratio": timing.deadline_miss_ratio,
        },
        "shutdown": dict(shutdown),
        "evidence": [dict(item) for item in evidence_index.links],
        "time_authority_observation": {
            "source_id": time_authority.source_id,
            "sample_count": time_authority.sample_count,
            "window_start_ns": time_authority.window_start_ns,
            "window_end_ns": time_authority.window_end_ns,
            "p50_delivery_latency_ms": time_authority.p50_ms,
            "p95_delivery_latency_ms": time_authority.p95_ms,
            "max_delivery_latency_ms": time_authority.max_ms,
            "within_policy": time_authority.within_policy,
            **(
                {"evidence_sha256": time_authority_evidence_sha256}
                if time_authority_evidence_sha256 is not None
                else {}
            ),
        },
    }
    if hardware_timing is not None:
        assert hardware_timing_evidence_sha256 is not None
        result["hardware_clock_observation"] = _hardware_clock_result(
            hardware_timing,
            hardware_timing_evidence_sha256,
        )
    if bundle.model is not None:
        result["model_manifest_sha256"] = bundle.model.sha256
    if bundle.dataset is not None:
        result["dataset_manifest_sha256"] = bundle.dataset.sha256
    validate_document(result)
    return result


def _temporary_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(name)


def write_contract_json(document: Mapping[str, Any], path: str | Path) -> Path:
    """Validate and atomically write a contract document as canonical JSON."""

    validate_document(document)
    destination = Path(path).expanduser().resolve()
    temporary_path = _temporary_path(destination)
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as temporary:
            json.dump(document, temporary, indent=2, sort_keys=True, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination


def write_junit_xml(result: Mapping[str, Any], path: str | Path) -> Path:
    """Write assertion outcomes in standard JUnit XML using junitparser."""

    validate_document(result)
    suite = TestSuite("robotics-acceptance")
    suite.add_property("scenario_sha256", result["scenario_sha256"])
    suite.add_property("runtime_manifest_sha256", result["runtime_manifest_sha256"])
    assertion_results = list(result["assertion_results"])
    forbidden = result["forbidden_graph_observation"]
    assertion_results.append(
        {
            "assertion_id": "forbidden-ros-graph",
            "status": "passed" if forbidden["passed"] else "failed",
            "message": "" if forbidden["passed"] else "forbidden ROS interface observed",
        }
    )
    hardware = result.get("hardware_clock_observation")
    if hardware is not None:
        assertion_results.append(
            {
                "assertion_id": "hardware-clock-policy",
                "status": "passed" if hardware["within_policy"] else "failed",
                "message": "" if hardware["within_policy"] else "hardware timing out of policy",
            }
        )
    time_authority = result.get("time_authority_observation")
    if time_authority is not None:
        assertion_results.append(
            {
                "assertion_id": "time-authority-policy",
                "status": "passed" if time_authority["within_policy"] else "failed",
                "message": (
                    ""
                    if time_authority["within_policy"]
                    else "time-authority evidence is out of policy"
                ),
            }
        )
    unevaluated = result.get("unevaluated", [])
    if unevaluated:
        assertion_results.append(
            {
                "assertion_id": "evaluation-coverage",
                "status": "failed",
                "message": f"unevaluated declarations: {', '.join(unevaluated)}",
            }
        )
    for assertion in assertion_results:
        case = TestCase(assertion["assertion_id"], classname="robotics.acceptance")
        message = assertion.get("message", "")
        if assertion["status"] == "failed":
            case.result = [Failure(message or "acceptance assertion failed")]
        elif assertion["status"] == "error":
            case.result = [Error(message or "acceptance assertion error")]
        elif assertion["status"] == "skipped":
            case.result = [Skipped(message or "acceptance assertion skipped")]
        suite.add_testcase(case)

    destination = Path(path).expanduser().resolve()
    temporary_path = _temporary_path(destination)
    try:
        xml = JUnitXml()
        xml.add_testsuite(suite)
        xml.write(str(temporary_path), pretty=True)
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination

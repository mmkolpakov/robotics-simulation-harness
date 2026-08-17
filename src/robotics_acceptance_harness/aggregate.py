from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from robotics_runtime_contracts import (
    ClockEvidenceValidationError,
    validate_clock_relation_evidence,
    worst_status,
)

from robotics_acceptance_harness import __version__
from robotics_acceptance_harness.documents import BundleValidationError, load_document
from robotics_acceptance_harness.evidence import load_evidence_index
from robotics_acceptance_harness.otel import OTLP_JSON_LINES_MEDIA_TYPE
from robotics_acceptance_harness.result import format_utc_datetime, write_contract_json
from robotics_acceptance_harness.timing import utc_datetime_from_unix_ns
from robotics_acceptance_harness.traces import (
    CausalHop,
    ChainViolation,
    evaluate_causal_chain,
    evaluate_channel_delivery,
    load_otlp_json_traces,
    validate_trace_set,
)


def aggregate_results(
    *,
    scenario_path: str | Path,
    run_context_path: str | Path,
    result_paths: Sequence[str | Path],
    output_path: str | Path,
    transport_qualification_path: str | Path | None = None,
    aggregate_id: str | None = None,
    generated_at: datetime | None = None,
    extension_schemas: Mapping[str, bytes | str] | None = None,
) -> Path:
    """Validate and aggregate the complete per-domain result registry for one run."""

    scenario = load_document(
        scenario_path,
        expected_role="acceptance_scenario",
        extension_schemas=extension_schemas,
    )
    context_path = Path(run_context_path).expanduser().resolve()
    context = load_document(context_path, expected_role="acceptance_run")
    if (
        context.data["scenario_id"] != scenario.data["scenario_id"]
        or context.data["scenario_sha256"] != scenario.sha256
    ):
        raise BundleValidationError("$.scenario_sha256", "run context identifies another scenario")
    if not result_paths:
        raise BundleValidationError("$.per_domain_results", "at least one result is required")

    resolved_result_paths = [Path(path).expanduser().resolve() for path in result_paths]
    results = [
        load_document(
            path,
            expected_role="acceptance_result",
        )
        for path in resolved_result_paths
    ]
    qualification_path = (
        Path(transport_qualification_path).expanduser().resolve()
        if transport_qualification_path is not None
        else None
    )
    qualification = (
        load_document(
            qualification_path,
            expected_role="transport_qualification_result",
        )
        if qualification_path is not None
        else None
    )
    expected_domains = {item["domain_id"] for item in context.data["domains"]}
    observed_domains = {item.data["domain_id"] for item in results}
    if observed_domains != expected_domains:
        missing = sorted(expected_domains - observed_domains)
        unexpected = sorted(observed_domains - expected_domains)
        raise BundleValidationError(
            "$.per_domain_results",
            f"domain registry mismatch; missing={missing}, unexpected={unexpected}",
        )

    result_ids = [item.data["result_id"] for item in results]
    if len(result_ids) != len(set(result_ids)):
        raise BundleValidationError("$.per_domain_results", "result_id values must be unique")
    for result in results:
        if result.data["run_id"] != context.data["run_id"]:
            raise BundleValidationError("$.run_id", "result belongs to another run")
        if result.data["scenario_id"] != context.data["scenario_id"]:
            raise BundleValidationError("$.scenario_id", "result belongs to another scenario")
        if result.data["scenario_sha256"] != context.data["scenario_sha256"]:
            raise BundleValidationError(
                "$.scenario_sha256",
                "result scenario digest differs from the run context",
            )
        if (
            result.data["time_authority_observation"]["source_id"]
            != context.data["time_authority"]["source_id"]
        ):
            raise BundleValidationError(
                "$.time_authority_observation.source_id",
                "result uses another time authority",
            )

    aggregate_status = worst_status({str(item.data["status"]) for item in results})
    if qualification is None:
        cross_domain: dict[str, Any] = {
            "status": "unevaluated",
            "reason": "transport_qualification_not_provided",
        }
    else:
        if qualification.data["run_id"] != context.data["run_id"]:
            raise BundleValidationError(
                "$.transport_qualification.run_id",
                "transport qualification belongs to another run",
            )
        if qualification.data["scenario_sha256"] != scenario.sha256:
            raise BundleValidationError(
                "$.transport_qualification.scenario_sha256",
                "transport qualification belongs to another scenario",
            )
        qualification_domains = {item["domain_id"] for item in qualification.data["trace_evidence"]}
        if qualification_domains != expected_domains:
            raise BundleValidationError(
                "$.transport_qualification.trace_evidence",
                "transport qualification must cover every run domain exactly once",
            )
        qualification_status = str(qualification.data["verdict"]["status"])
        cross_domain = {
            "status": worst_status(
                {aggregate_status, qualification_status},
                collapse_cancelled=True,
            ),
            "transport_qualification": {
                "qualification_id": qualification.data["qualification_id"],
                "result_sha256": qualification.sha256,
                "status": qualification_status,
            },
        }
    aggregate: dict[str, Any] = {
        "schema_version": "acceptance-aggregate.v1",
        "aggregate_id": aggregate_id or f"aggregate-{uuid4()}",
        "run_id": context.data["run_id"],
        "acceptance_run_sha256": context.sha256,
        "generated_at": format_utc_datetime(generated_at or datetime.now(UTC)),
        "per_domain_results": [
            {
                "domain_id": result.data["domain_id"],
                "result_id": result.data["result_id"],
                "result_sha256": result.sha256,
                "status": result.data["status"],
            }
            for result in sorted(results, key=lambda item: item.data["domain_id"])
        ],
        "per_domain_aggregate": aggregate_status,
        "cross_domain_e2e": cross_domain,
    }
    context_digest = sha256(context_path.read_bytes()).hexdigest()
    if context_digest != context.sha256:
        raise BundleValidationError(
            "$.acceptance_run_sha256",
            "run context changed during aggregation",
        )
    for index, (path, result) in enumerate(zip(resolved_result_paths, results, strict=True)):
        if sha256(path.read_bytes()).hexdigest() != result.sha256:
            raise BundleValidationError(
                f"$.per_domain_results[{index}].result_sha256",
                "domain result changed during aggregation",
            )
    if (
        qualification is not None
        and qualification_path is not None
        and sha256(qualification_path.read_bytes()).hexdigest() != qualification.sha256
    ):
        raise BundleValidationError(
            "$.transport_qualification.result_sha256",
            "transport qualification changed during aggregation",
        )
    return write_contract_json(aggregate, output_path)


def _trace_link(
    *,
    domain_id: str,
    verified_index_sha256: str,
    link: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "domain_id": domain_id,
        "evidence_index_sha256": verified_index_sha256,
        "artifact_id": link["artifact_id"],
        **({"segment_index": link["segment_index"]} if "segment_index" in link else {}),
        "uri": link["uri"],
        "media_type": link["media_type"],
        "format": "otlp-jsonl",
        "signal": "traces",
        "sha256": link["sha256"],
        "size_bytes": link["size_bytes"],
    }


def _chain_violation_document(violation: ChainViolation) -> dict[str, Any]:
    return {
        "code": violation.code,
        **({"channel_id": violation.channel_id} if violation.channel_id else {}),
        "message": violation.message,
    }


def _channel_violation_document(violation: ChainViolation) -> dict[str, str]:
    return {
        "code": violation.code,
        "message": violation.message,
    }


def _span_reference(span: Any) -> dict[str, Any]:
    if span.message_id is None:
        raise BundleValidationError(
            "$.causal_chains.hops",
            "a verified causal hop must carry messaging.message.id",
        )
    return {
        "domain_id": span.domain_id,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "message_id": span.message_id,
    }


def _hop_document(hop: CausalHop) -> dict[str, Any]:
    return {
        "channel_id": hop.channel_id,
        "relationship": hop.relationship,
        "producer": _span_reference(hop.producer),
        "consumer": _span_reference(hop.consumer),
        "status": "passed",
        "violations": [],
    }


def evaluate_transport_qualification(
    *,
    run_id: str,
    scenario_path: str | Path,
    causal_chain_paths: Sequence[str | Path],
    channel_contract_paths: Sequence[str | Path],
    trace_paths: Mapping[str, str | Path],
    evidence_index_paths: Mapping[str, str | Path],
    artifact_receipt_paths: Mapping[str, Sequence[str | Path]] | None = None,
    artifact_verification_paths: Mapping[str, Sequence[str | Path]] | None = None,
    receipt_dependency_paths: Mapping[str, Sequence[str | Path]] | None = None,
    clock_relation_paths: Sequence[str | Path] = (),
    observation_output_dir: str | Path,
    output_path: str | Path,
    qualification_id: str | None = None,
    generated_at: datetime | None = None,
    extension_schemas: Mapping[str, bytes | str] | None = None,
) -> Path:
    """Evaluate transport evidence without inventing a domain execution."""

    evaluated_at = generated_at or datetime.now(UTC)
    scenario = load_document(
        scenario_path,
        expected_role="acceptance_scenario",
        extension_schemas=extension_schemas,
    )
    clock_policy = scenario.data["time_policy"].get("cross_domain_clock")
    if clock_policy is None:
        raise BundleValidationError(
            "$.time_policy.cross_domain_clock",
            "cross-domain transport requires an explicit clock policy",
        )
    if not causal_chain_paths:
        raise BundleValidationError(
            "$.causal_chain_contracts",
            "at least one causal-chain contract is required",
        )
    chain_contracts = [
        load_document(path, expected_role="causal_chain") for path in causal_chain_paths
    ]
    chain_ids = [str(item.data["chain_id"]) for item in chain_contracts]
    if len(chain_ids) != len(set(chain_ids)):
        raise BundleValidationError(
            "$.causal_chain_contracts",
            "chain_id values must be unique",
        )
    expected_domains = tuple(sorted(trace_paths))
    expected_domain_set = set(expected_domains)
    if set(evidence_index_paths) != expected_domain_set:
        raise BundleValidationError(
            "$.trace_evidence",
            "evidence-index mapping must contain every run domain exactly once",
        )

    channel_contracts = [
        load_document(path, expected_role="transport_channel") for path in channel_contract_paths
    ]
    channel_ids = [str(item.data["channel_id"]) for item in channel_contracts]
    if len(channel_ids) != len(set(channel_ids)):
        raise BundleValidationError(
            "$.channel_contracts",
            "channel_id values must be unique",
        )
    channel_by_id = {str(item.data["channel_id"]): item for item in channel_contracts}
    chain_channels: dict[str, list[Any]] = {}
    referenced_channel_ids: set[str] = set()
    for chain_contract in chain_contracts:
        ordered_contracts: list[Any] = []
        for reference in chain_contract.data["channel_contracts"]:
            channel_id = str(reference["channel_id"])
            contract = channel_by_id.get(channel_id)
            if contract is None or contract.sha256 != reference["sha256"]:
                raise BundleValidationError(
                    f"$.causal_chain_contracts.{chain_contract.data['chain_id']}",
                    "referenced channel contract is absent or has another digest",
                )
            ordered_contracts.append(contract)
            referenced_channel_ids.add(channel_id)
        touched_domains = {
            str(contract.data[endpoint]["domain_id"])
            for contract in ordered_contracts
            for endpoint in ("source", "destination")
        }
        required_domains = {
            str(domain_id) for domain_id in chain_contract.data["required_domain_ids"]
        }
        if required_domains != touched_domains:
            raise BundleValidationError(
                f"$.causal_chain_contracts.{chain_contract.data['chain_id']}.required_domain_ids",
                "must equal the domains touched by the referenced channels",
            )
        chain_channels[str(chain_contract.data["chain_id"])] = ordered_contracts
    if referenced_channel_ids != set(channel_by_id):
        raise BundleValidationError(
            "$.channel_contracts",
            "every supplied channel contract must belong to a causal chain",
        )
    for contract in channel_contracts:
        contract_domains = {
            str(contract.data["source"]["domain_id"]),
            str(contract.data["destination"]["domain_id"]),
        }
        if not contract_domains <= expected_domain_set:
            raise BundleValidationError(
                f"$.channel_contracts.{contract.data['channel_id']}",
                "channel references a domain outside the acceptance run",
            )
    all_contract_domains = {
        str(contract.data[endpoint]["domain_id"])
        for contract in channel_contracts
        for endpoint in ("source", "destination")
    }
    if all_contract_domains != expected_domain_set:
        raise BundleValidationError(
            "$.channel_contracts",
            "causal chains must collectively traverse every acceptance-run domain",
        )

    trace_evidence: list[dict[str, Any]] = []
    spans_by_domain = {}
    verified_by_domain = {}
    for domain_id in sorted(expected_domains):
        verified = load_evidence_index(
            evidence_index_paths[domain_id],
            expected_run_id=run_id,
            receipt_paths=(artifact_receipt_paths or {}).get(domain_id, ()),
            verification_paths=(artifact_verification_paths or {}).get(domain_id, ()),
            receipt_dependency_paths=(receipt_dependency_paths or {}).get(domain_id, ()),
        )
        verified_by_domain[domain_id] = verified
        trace_path = Path(trace_paths[domain_id]).expanduser().resolve()
        link = verified.local_files.get(trace_path)
        if link is None or link["media_type"] != OTLP_JSON_LINES_MEDIA_TYPE:
            raise BundleValidationError(
                f"$.trace_evidence.{domain_id}",
                f"trace file must be verified local {OTLP_JSON_LINES_MEDIA_TYPE} evidence",
            )
        trace_evidence.append(
            _trace_link(
                domain_id=domain_id,
                verified_index_sha256=verified.index.sha256,
                link=link,
            )
        )
        spans_by_domain[domain_id] = load_otlp_json_traces(
            trace_path,
            expected_run_id=run_id,
            expected_domain_id=domain_id,
            expected_sha256=str(link["sha256"]),
        )
    validate_trace_set(spans_by_domain)

    clock_relations = [
        load_document(path, expected_role="clock_relation") for path in clock_relation_paths
    ]
    clock_relation_references: list[dict[str, Any]] = []
    clock_statuses: set[str] = set()
    required_clock_pairs = {
        (
            str(contract.data["source"]["domain_id"]),
            str(contract.data["destination"]["domain_id"]),
        )
        for contract in channel_contracts
    }
    observed_clock_pairs: set[tuple[str, str]] = set()
    evidence_sha256_by_domain = {
        domain_id: frozenset(str(link["sha256"]) for link in verified.links)
        for domain_id, verified in verified_by_domain.items()
    }
    for relation in clock_relations:
        if relation.data["run_id"] != run_id:
            raise BundleValidationError(
                "$.clock_relations",
                "clock relation belongs to another run",
            )
        if relation.data["scenario_sha256"] != scenario.sha256:
            raise BundleValidationError(
                "$.clock_relations",
                "clock relation belongs to another scenario",
            )
        if dict(relation.data["policy"]) != dict(clock_policy):
            raise BundleValidationError(
                "$.clock_relations",
                "clock relation policy differs from the scenario",
            )
        pair = (
            str(relation.data["source_domain_id"]),
            str(relation.data["destination_domain_id"]),
        )
        if pair not in required_clock_pairs or pair in observed_clock_pairs:
            raise BundleValidationError(
                "$.clock_relations",
                "clock relation domain pairs must uniquely match channel directions",
            )
        observed_clock_pairs.add(pair)
        try:
            validate_clock_relation_evidence(
                relation.data,
                evidence_sha256_by_domain,
            )
        except ClockEvidenceValidationError as error:
            raise BundleValidationError(
                "$.clock_relations",
                str(error),
            ) from error
        clock_relation_references.append(
            {
                "relation_id": relation.data["relation_id"],
                "source_domain_id": relation.data["source_domain_id"],
                "destination_domain_id": relation.data["destination_domain_id"],
                "sha256": relation.sha256,
                "status": relation.data["status"],
            }
        )
        clock_statuses.add(str(relation.data["status"]))
    if observed_clock_pairs != required_clock_pairs:
        clock_statuses.add("incomplete")

    observation_dir = Path(observation_output_dir).expanduser().resolve()
    observation_dir.mkdir(parents=True, exist_ok=True)
    observation_references: list[dict[str, Any]] = []
    observation_statuses: set[str] = set()
    for contract in sorted(
        channel_contracts,
        key=lambda item: str(item.data["channel_id"]),
    ):
        channel_id = str(contract.data["channel_id"])
        observation = evaluate_channel_delivery(contract.data, spans_by_domain)
        if observation.started_at_ns:
            started_at = utc_datetime_from_unix_ns(observation.started_at_ns)
            finished_at = utc_datetime_from_unix_ns(observation.finished_at_ns)
        else:
            finished_at = evaluated_at
            started_at = evaluated_at - timedelta(
                seconds=float(contract.data["delivery"]["observation_window_sec"])
            )
        observation_document = {
            "schema_version": "transport-channel-observation.v1",
            "observation_id": f"observation-{uuid4()}",
            "run_id": run_id,
            "channel_id": channel_id,
            "channel_contract_sha256": contract.sha256,
            "started_at": format_utc_datetime(started_at),
            "finished_at": format_utc_datetime(finished_at),
            "sent_count": observation.sent_count,
            "received_count": observation.received_count,
            "lost_count": observation.lost_count,
            "duplicate_count": observation.duplicate_count,
            "out_of_order_count": observation.out_of_order_count,
            "loss_ratio": observation.loss_ratio,
            "max_message_age_ms": observation.max_message_age_ms,
            "status": observation.status,
            "violations": [
                _channel_violation_document(violation) for violation in observation.violations
            ],
        }
        observation_path = write_contract_json(
            observation_document,
            observation_dir / f"{channel_id}.json",
        )
        observation_digest = sha256(observation_path.read_bytes()).hexdigest()
        observation_references.append(
            {
                "channel_id": channel_id,
                "observation_id": observation_document["observation_id"],
                "sha256": observation_digest,
                "status": observation.status,
            }
        )
        observation_statuses.add(observation.status)

    chain_documents: list[dict[str, Any]] = []
    chain_statuses: set[str] = set()
    for chain_contract in sorted(
        chain_contracts,
        key=lambda item: str(item.data["chain_id"]),
    ):
        chain = evaluate_causal_chain(
            chain_contract.data,
            [item.data for item in chain_channels[str(chain_contract.data["chain_id"])]],
            spans_by_domain,
        )
        chain_document: dict[str, Any] = {
            "chain_id": chain_contract.data["chain_id"],
            "expected_contract_sha256": chain_contract.sha256,
            "channel_ids": list(chain.channel_ids),
            "status": chain.status,
            "hops": [_hop_document(hop) for hop in chain.hops],
            "violations": [_chain_violation_document(violation) for violation in chain.violations],
        }
        if chain.root_trace_id is not None:
            chain_document["root_trace_id"] = chain.root_trace_id
        if chain.trace_ids:
            chain_document["trace_ids"] = list(chain.trace_ids)
        chain_documents.append(chain_document)
        chain_statuses.add(chain.status)

    common: dict[str, Any] = {
        "evaluator": {
            "implementation": "robotics-acceptance-harness",
            "version": __version__,
        },
        "trace_evidence": trace_evidence,
        "causal_chain_contracts": [
            {
                "chain_id": chain_contract.data["chain_id"],
                "sha256": chain_contract.sha256,
            }
            for chain_contract in sorted(
                chain_contracts,
                key=lambda item: str(item.data["chain_id"]),
            )
        ],
        "channel_contracts": [
            {
                "channel_id": contract.data["channel_id"],
                "source_domain_id": contract.data["source"]["domain_id"],
                "destination_domain_id": contract.data["destination"]["domain_id"],
                "sha256": contract.sha256,
            }
            for contract in sorted(
                channel_contracts,
                key=lambda item: str(item.data["channel_id"]),
            )
        ],
        "channel_observations": observation_references,
        "causal_chains": chain_documents,
    }
    transport_status = worst_status(
        {*observation_statuses, *chain_statuses, *clock_statuses},
        collapse_cancelled=True,
    )
    verdict = {
        "status": transport_status,
        "evaluated_at": format_utc_datetime(evaluated_at),
        "chain_count": len(chain_documents),
        "passed_chain_count": sum(item["status"] == "passed" for item in chain_documents),
        "failed_chain_count": sum(item["status"] == "failed" for item in chain_documents),
        "incomplete_chain_count": sum(item["status"] == "incomplete" for item in chain_documents),
        "error_chain_count": sum(item["status"] == "error" for item in chain_documents),
    }

    result: dict[str, Any] = {
        "schema_version": "transport-qualification-result.v1",
        "qualification_id": qualification_id or f"qualification-{uuid4()}",
        "run_id": run_id,
        "scenario_sha256": scenario.sha256,
        "generated_at": format_utc_datetime(evaluated_at),
        **common,
        "clock_relations": sorted(
            clock_relation_references,
            key=lambda item: (
                item["source_domain_id"],
                item["destination_domain_id"],
            ),
        ),
        "verdict": verdict,
    }
    return write_contract_json(result, output_path)

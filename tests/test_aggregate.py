from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from robotics_runtime_contracts import validate_document

from robotics_acceptance_harness.aggregate import (
    aggregate_results,
    evaluate_transport_qualification,
)
from robotics_acceptance_harness.documents import BundleValidationError, load_bundle, load_document
from robotics_acceptance_harness.evidence import load_evidence_index
from robotics_acceptance_harness.forbidden_graph import ForbiddenGraphObservation
from robotics_acceptance_harness.metrics import AssertionEvaluation
from robotics_acceptance_harness.readiness import GraphSnapshot, ReadinessResult
from robotics_acceptance_harness.result import (
    build_acceptance_result,
    write_contract_json,
)
from robotics_acceptance_harness.time_authority import TimeAuthorityObservation
from robotics_acceptance_harness.timing import TimingObservation
from robotics_acceptance_harness.traces import TraceInputError
from tests.support import acceptance_run, local_evidence_artifact, write_evidence_index

FIXTURES = Path(__file__).parent / "fixtures" / "simulation"
RUN_ID = "run-01234567-89ab-4def-8123-456789abcdef"
TRACE_ID = "01" * 16
TYPE_HASH = f"RIHS01_{'1' * 64}"


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def transport_scenario(tmp_path: Path) -> Path:
    scenario = yaml.safe_load((FIXTURES / "scenario.yaml").read_text(encoding="utf-8"))
    scenario["schema_version"] = "acceptance-scenario.v1"
    scenario["metric_definitions"] = []
    scenario["time_policy"]["cross_domain_clock"] = {
        "method": "measured_skew",
        "minimum_samples": 30,
        "maximum_absolute_skew_ms": 1,
    }
    path = tmp_path / "transport-scenario.yaml"
    path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return path


def clock_relation(tmp_path: Path, scenario_path: Path, evidence_path: Path) -> Path:
    return write_json(
        tmp_path / "clock-relation.json",
        {
            "schema_version": "clock-relation.v1",
            "relation_id": "camera-to-control-clock",
            "run_id": RUN_ID,
            "scenario_sha256": hashlib.sha256(scenario_path.read_bytes()).hexdigest(),
            "source_domain_id": "camera-domain",
            "destination_domain_id": "control-domain",
            "method": "measured_skew",
            "sync_protocol": "sim_clock",
            "started_at": "2026-07-26T12:00:00Z",
            "finished_at": "2026-07-26T12:00:01Z",
            "sample_count": 30,
            "max_absolute_skew_ms": 0.1,
            "policy": {
                "method": "measured_skew",
                "minimum_samples": 30,
                "maximum_absolute_skew_ms": 1,
            },
            "status": "passed",
            "violations": [],
            "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        },
    )


def result(
    tmp_path: Path,
    domain_id: str,
    suffix: str,
    *,
    assertion_status: str = "passed",
    unevaluated: tuple[str, ...] = (),
    time_authority_within_policy: bool = True,
) -> Path:
    bundle = load_bundle(
        FIXTURES / "scenario.yaml",
        runtime_path=FIXTURES / "runtime.yaml",
    )
    evidence_payload_path = write_json(
        tmp_path / f"metrics-{suffix}.json",
        {"domain_id": domain_id},
    )
    artifact = local_evidence_artifact(
        evidence_payload_path,
        media_type="application/x-ndjson",
    )
    evidence_path = write_evidence_index(
        tmp_path / f"evidence-{suffix}.json",
        run_id=RUN_ID,
        artifacts=[artifact],
        recording_mode="bounded",
    )
    document = build_acceptance_result(
        result_id=f"result-01234567-89ab-4def-8123-456789abcde{suffix}",
        run_id=RUN_ID,
        domain_id=domain_id,
        bundle=bundle,
        readiness=ReadinessResult(GraphSnapshot(1), 1, 0),
        timing=TimingObservation(True, 0, 0, 1, 0, 0, 1),
        time_authority=TimeAuthorityObservation(
            "simulation-clock",
            30,
            1,
            30,
            0,
            0,
            0,
            time_authority_within_policy,
        ),
        time_authority_evidence_sha256=artifact["sha256"],
        assertions=(
            AssertionEvaluation(
                assertion_id="domain-smoke",
                status=assertion_status,
                observed_value=1,
                unit="1",
            ),
        ),
        unevaluated=unevaluated,
        started_at=datetime(2026, 7, 26, 12, tzinfo=UTC),
        finished_at=datetime(2026, 7, 26, 12, 1, tzinfo=UTC),
        monotonic_duration_sec=60,
        shutdown={
            "observer_detached": True,
            "recorders_closed": True,
            "evidence_index_finalized": True,
        },
        evidence_index=load_evidence_index(evidence_path),
        forbidden_graph=ForbiddenGraphObservation((), (), (), ()),
    )
    return write_contract_json(document, tmp_path / f"result-{suffix}.json")


def run_context(
    tmp_path: Path,
    domains: list[dict[str, str]] | None = None,
) -> Path:
    bundle = load_bundle(
        FIXTURES / "scenario.yaml",
        runtime_path=FIXTURES / "runtime.yaml",
    )
    return write_json(
        tmp_path / "acceptance-run.json",
        acceptance_run(
            run_id=RUN_ID,
            scenario_id=str(bundle.scenario.data["scenario_id"]),
            scenario_sha256=bundle.scenario.sha256,
            time_kind="sim_clock",
            source_id="simulation-clock",
            domains=domains
            or [
                {"domain_id": "camera-domain", "role": "sensor"},
                {"domain_id": "control-domain", "role": "controller"},
            ],
        ),
    )


def base_aggregate(tmp_path: Path, context_path: Path) -> Path:
    return aggregate_results(
        scenario_path=FIXTURES / "scenario.yaml",
        run_context_path=context_path,
        result_paths=[
            result(tmp_path, "camera-domain", "0"),
            result(tmp_path, "control-domain", "1"),
        ],
        output_path=tmp_path / "domain-aggregate.json",
        aggregate_id="aggregate-01234567-89ab-4def-8123-456789abcdea",
        generated_at=datetime(2026, 7, 26, 12, 2, tzinfo=UTC),
    )


def trace_file(
    tmp_path: Path,
    domain_id: str,
    span_name: str,
    span_byte: int,
    *,
    message_id: str | None = "message-1",
    parent_byte: int | None = None,
    link_byte: int | None = None,
) -> Path:
    attributes = (
        [
            {
                "key": "messaging.message.id",
                "value": {"stringValue": message_id},
            }
        ]
        if message_id is not None
        else []
    )
    span: dict[str, object] = {
        "traceId": TRACE_ID,
        "spanId": f"{span_byte:02x}" * 8,
        "name": span_name,
        "kind": 1,
        "startTimeUnixNano": (
            "1785067200000003000"
            if parent_byte is not None or link_byte is not None
            else "1785067200000001000"
        ),
        "endTimeUnixNano": (
            "1785067200000004000"
            if parent_byte is not None or link_byte is not None
            else "1785067200000002000"
        ),
        "attributes": attributes,
    }
    if parent_byte is not None:
        span["parentSpanId"] = f"{parent_byte:02x}" * 8
    if link_byte is not None:
        span["links"] = [
            {
                "traceId": TRACE_ID,
                "spanId": f"{link_byte:02x}" * 8,
                "attributes": attributes,
            }
        ]
    return write_json(
        tmp_path / f"{domain_id}.traces.jsonl",
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "run.id",
                                "value": {"stringValue": RUN_ID},
                            },
                            {
                                "key": "domain.id",
                                "value": {"stringValue": domain_id},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "robotics.test"},
                            "spans": [span],
                        }
                    ],
                }
            ]
        },
    )


def trace_evidence_index(
    tmp_path: Path,
    domain_id: str,
    trace_path: Path,
    *,
    extra_paths: tuple[Path, ...] = (),
) -> Path:
    return write_evidence_index(
        tmp_path / f"{domain_id}.evidence.json",
        run_id=RUN_ID,
        recording_mode="bounded",
        artifacts=[
            local_evidence_artifact(
                trace_path,
                media_type="application/x-ndjson",
                artifact_index=900000,
            ),
            *(
                local_evidence_artifact(
                    path,
                    media_type="application/json",
                    artifact_index=900001 + index,
                )
                for index, path in enumerate(extra_paths)
            ),
        ],
    )


def channel_contract(tmp_path: Path, relationship: str = "link") -> Path:
    return write_json(
        tmp_path / "channel.json",
        {
            "schema_version": "transport-channel.v1",
            "channel_id": "sensor.control",
            "source": {
                "domain_id": "camera-domain",
                "ros_domain_id": 10,
                "topic": "/observations",
                "message_type": "example_interfaces/msg/String",
                "type_hash": TYPE_HASH,
            },
            "destination": {
                "domain_id": "control-domain",
                "ros_domain_id": 20,
                "topic": "/observations",
                "message_type": "example_interfaces/msg/String",
                "type_hash": TYPE_HASH,
            },
            "implementation_binding": {
                "implementation_id": "reference-bridge",
                "version": "1.9.0",
                "configuration_sha256": "2" * 64,
                "options": {
                    "discovery_scope": "local_domain_only",
                    "route": "robotics/observations",
                },
            },
            "qos": {
                "reliability": "reliable",
                "durability": "volatile",
                "history": "keep_last",
                "depth": 10,
                "liveliness": "automatic",
                "liveliness_lease_duration_ms": "infinite",
                "deadline_ms": 100,
                "lifespan_ms": 500,
            },
            "delivery": {
                "observation_window_sec": 30,
                "minimum_source_messages": 1,
                "message_id_attribute": "messaging.message.id",
                "max_loss_ratio": 0,
                "max_duplicate_count": 0,
                "max_out_of_order_count": 0,
                "max_message_age_ms": 100,
            },
            "trace": {
                "carrier_field": "trace_context",
                "relationship": relationship,
                "producer_span_name": "observation publish",
                "consumer_span_name": "observation receive",
            },
        },
    )


def causal_chain(
    tmp_path: Path,
    channel_path: Path,
    *,
    chain_id: str = "sensor-to-control",
    filename: str = "causal-chain.json",
) -> Path:
    return write_json(
        tmp_path / filename,
        {
            "schema_version": "causal-chain.v1",
            "chain_id": chain_id,
            "required_domain_ids": ["camera-domain", "control-domain"],
            "channel_contracts": [
                {
                    "channel_id": "sensor.control",
                    "sha256": hashlib.sha256(channel_path.read_bytes()).hexdigest(),
                }
            ],
            "require_connected_trace_graph": True,
            "missing_evidence_status": "incomplete",
            "broken_relationship_status": "failed",
        },
    )


def transport_qualification(
    tmp_path: Path,
    *,
    relationship: str,
    consumer_parent: int | None = None,
    consumer_link: int | None = None,
    consumer_span_byte: int = 3,
    producer_message_id: str | None = "message-1",
    consumer_message_id: str | None = "message-1",
    producer_span_name: str = "observation publish",
    consumer_span_name: str = "observation receive",
    chain_count: int = 1,
) -> dict[str, object]:
    scenario_path = transport_scenario(tmp_path)
    producer = trace_file(
        tmp_path,
        "camera-domain",
        producer_span_name,
        2,
        message_id=producer_message_id,
    )
    consumer = trace_file(
        tmp_path,
        "control-domain",
        consumer_span_name,
        consumer_span_byte,
        message_id=consumer_message_id,
        parent_byte=consumer_parent,
        link_byte=consumer_link,
    )
    channel_path = channel_contract(tmp_path, relationship)
    chain_paths = [causal_chain(tmp_path, channel_path)]
    if chain_count == 2:
        chain_paths.append(
            causal_chain(
                tmp_path,
                channel_path,
                chain_id="sensor-to-control-audit",
                filename="causal-chain-audit.json",
            )
        )
    output = evaluate_transport_qualification(
        run_id=RUN_ID,
        scenario_path=scenario_path,
        causal_chain_paths=chain_paths,
        channel_contract_paths=[channel_path],
        trace_paths={
            "camera-domain": producer,
            "control-domain": consumer,
        },
        evidence_index_paths={
            "camera-domain": trace_evidence_index(tmp_path, "camera-domain", producer),
            "control-domain": trace_evidence_index(tmp_path, "control-domain", consumer),
        },
        clock_relation_paths=(clock_relation(tmp_path, scenario_path, producer),),
        observation_output_dir=tmp_path / "transport-observations",
        output_path=tmp_path / "transport-qualification.json",
        qualification_id="qualification-01234567-89ab-4def-8123-456789abcdef",
        generated_at=datetime(2026, 7, 26, 12, 3, tzinfo=UTC),
    )
    return json.loads(output.read_text(encoding="utf-8"))


def transport_aggregate_inputs(
    tmp_path: Path,
    scenario_path: Path,
) -> tuple[Path, list[Path]]:
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    scenario_sha256 = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
    context_path = write_json(
        tmp_path / "acceptance-run.json",
        acceptance_run(
            run_id=RUN_ID,
            scenario_id=scenario["scenario_id"],
            scenario_sha256=scenario_sha256,
            time_kind="sim_clock",
            source_id="simulation-clock",
            domains=[
                {"domain_id": "camera-domain", "role": "sensor"},
                {"domain_id": "control-domain", "role": "controller"},
            ],
        ),
    )
    results = [result(tmp_path, "camera-domain", "0"), result(tmp_path, "control-domain", "1")]
    for result_path in results:
        result_document = json.loads(result_path.read_text(encoding="utf-8"))
        result_document["scenario_id"] = scenario["scenario_id"]
        result_document["scenario_sha256"] = scenario_sha256
        write_json(result_path, result_document)
    return context_path, results


def test_aggregate_requires_and_emits_every_registered_domain(tmp_path: Path) -> None:
    context_path = run_context(tmp_path)
    output = base_aggregate(tmp_path, context_path)

    aggregate = json.loads(output.read_text(encoding="utf-8"))
    assert aggregate["per_domain_aggregate"] == "passed"
    assert [item["domain_id"] for item in aggregate["per_domain_results"]] == [
        "camera-domain",
        "control-domain",
    ]
    assert aggregate["cross_domain_e2e"]["status"] == "unevaluated"


def test_aggregate_references_transport_qualification(tmp_path: Path) -> None:
    transport_qualification(tmp_path, relationship="link", consumer_link=2)
    scenario_path = tmp_path / "transport-scenario.yaml"
    context_path, results = transport_aggregate_inputs(tmp_path, scenario_path)
    qualification_path = tmp_path / "transport-qualification.json"

    output = aggregate_results(
        scenario_path=scenario_path,
        run_context_path=context_path,
        result_paths=results,
        transport_qualification_path=qualification_path,
        output_path=tmp_path / "aggregate.json",
    )

    aggregate = json.loads(output.read_text(encoding="utf-8"))
    reference = aggregate["cross_domain_e2e"]["transport_qualification"]
    assert aggregate["schema_version"] == "acceptance-aggregate.v1"
    assert aggregate["cross_domain_e2e"]["status"] == "passed"
    assert reference["status"] == "passed"
    assert reference["result_sha256"] == hashlib.sha256(qualification_path.read_bytes()).hexdigest()


def test_shared_clock_observations_must_match_endpoint_domains(tmp_path: Path) -> None:
    scenario_path = transport_scenario(tmp_path)
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    scenario["time_policy"]["cross_domain_clock"] = {"method": "shared_clock_identity"}
    scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    producer = trace_file(tmp_path, "camera-domain", "observation publish", 2)
    consumer = trace_file(
        tmp_path,
        "control-domain",
        "observation receive",
        3,
        link_byte=2,
    )
    source_clock = write_json(tmp_path / "source-clock.json", {"domain": "camera-domain"})
    destination_clock = write_json(
        tmp_path / "destination-clock.json",
        {"domain": "control-domain"},
    )
    identity = {
        "authority": "shared-linux-kernel-clock-realtime",
        "boot_id": "01234567-89ab-4def-8123-456789abcdef",
        "implementation": "clock_gettime(CLOCK_REALTIME)",
        "resolution_sec": 1e-9,
        "source_observation_sha256": hashlib.sha256(destination_clock.read_bytes()).hexdigest(),
        "destination_observation_sha256": hashlib.sha256(source_clock.read_bytes()).hexdigest(),
    }
    identity_path = write_json(tmp_path / "shared-clock.json", identity)
    relation_path = write_json(
        tmp_path / "clock-relation.json",
        {
            "schema_version": "clock-relation.v1",
            "relation_id": "camera-to-control-clock",
            "run_id": RUN_ID,
            "scenario_sha256": hashlib.sha256(scenario_path.read_bytes()).hexdigest(),
            "source_domain_id": "camera-domain",
            "destination_domain_id": "control-domain",
            "method": "shared_clock_identity",
            "sync_protocol": "shared_kernel_clock",
            "started_at": "2026-07-26T12:00:00Z",
            "finished_at": "2026-07-26T12:00:01Z",
            "policy": {"method": "shared_clock_identity"},
            "shared_clock_identity": identity,
            "status": "passed",
            "violations": [],
            "evidence_sha256": hashlib.sha256(identity_path.read_bytes()).hexdigest(),
        },
    )
    channel_path = channel_contract(tmp_path)

    with pytest.raises(BundleValidationError, match="source_observation.*camera-domain"):
        evaluate_transport_qualification(
            run_id=RUN_ID,
            scenario_path=scenario_path,
            causal_chain_paths=[causal_chain(tmp_path, channel_path)],
            channel_contract_paths=[channel_path],
            trace_paths={"camera-domain": producer, "control-domain": consumer},
            evidence_index_paths={
                "camera-domain": trace_evidence_index(
                    tmp_path,
                    "camera-domain",
                    producer,
                    extra_paths=(source_clock, identity_path),
                ),
                "control-domain": trace_evidence_index(
                    tmp_path,
                    "control-domain",
                    consumer,
                    extra_paths=(destination_clock,),
                ),
            },
            clock_relation_paths=[relation_path],
            observation_output_dir=tmp_path / "transport-observations",
            output_path=tmp_path / "transport-qualification.json",
        )


def test_aggregate_rejects_transport_for_another_run(tmp_path: Path) -> None:
    context_path = run_context(tmp_path)
    transport_qualification(tmp_path, relationship="link", consumer_link=2)
    qualification_path = tmp_path / "transport-qualification.json"
    qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
    qualification["run_id"] = "run-01234567-89ab-4def-8123-456789abcdea"
    write_json(qualification_path, qualification)

    with pytest.raises(BundleValidationError, match="another run"):
        aggregate_results(
            scenario_path=FIXTURES / "scenario.yaml",
            run_context_path=context_path,
            result_paths=[
                result(tmp_path, "camera-domain", "0"),
                result(tmp_path, "control-domain", "1"),
            ],
            transport_qualification_path=qualification_path,
            output_path=tmp_path / "aggregate.json",
        )


def test_aggregate_rejects_transport_for_another_scenario(tmp_path: Path) -> None:
    transport_qualification(tmp_path, relationship="link", consumer_link=2)
    scenario_path = tmp_path / "transport-scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    scenario["scenario_id"] = "org.example.other-physics-smoke"
    scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    context_path, results = transport_aggregate_inputs(tmp_path, scenario_path)

    with pytest.raises(BundleValidationError, match="another scenario"):
        aggregate_results(
            scenario_path=scenario_path,
            run_context_path=context_path,
            result_paths=results,
            transport_qualification_path=tmp_path / "transport-qualification.json",
            output_path=tmp_path / "aggregate.json",
        )


def test_aggregate_rejects_transport_with_another_domain_set(tmp_path: Path) -> None:
    transport_qualification(tmp_path, relationship="link", consumer_link=2)
    scenario_path = tmp_path / "transport-scenario.yaml"
    context_path, results = transport_aggregate_inputs(tmp_path, scenario_path)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["domains"] = [{"domain_id": "camera-domain", "role": "sensor"}]
    write_json(context_path, context)

    with pytest.raises(BundleValidationError, match="every run domain"):
        aggregate_results(
            scenario_path=scenario_path,
            run_context_path=context_path,
            result_paths=[results[0]],
            transport_qualification_path=tmp_path / "transport-qualification.json",
            output_path=tmp_path / "aggregate.json",
        )


def test_aggregate_fails_when_registered_domain_has_no_result(tmp_path: Path) -> None:
    with pytest.raises(BundleValidationError, match="control-domain"):
        aggregate_results(
            scenario_path=FIXTURES / "scenario.yaml",
            run_context_path=run_context(tmp_path),
            result_paths=[result(tmp_path, "camera-domain", "0")],
            output_path=tmp_path / "aggregate.json",
        )


@pytest.mark.parametrize("assertion_status", ["error", "failed"])
def test_unevaluated_does_not_mask_known_failure(
    tmp_path: Path,
    assertion_status: str,
) -> None:
    result_path = result(
        tmp_path,
        "camera-domain",
        "0",
        assertion_status=assertion_status,
        unevaluated=("$.assertions.pending",),
    )

    document = json.loads(result_path.read_text(encoding="utf-8"))

    assert document["status"] == assertion_status


def test_unevaluated_does_not_mask_time_authority_failure(tmp_path: Path) -> None:
    result_path = result(
        tmp_path,
        "camera-domain",
        "0",
        unevaluated=("$.assertions.pending",),
        time_authority_within_policy=False,
    )

    document = json.loads(result_path.read_text(encoding="utf-8"))

    assert document["status"] == "failed"


def test_aggregate_rejects_result_changed_after_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = run_context(tmp_path)
    camera_result = result(tmp_path, "camera-domain", "0")
    control_result = result(tmp_path, "control-domain", "1")
    original_load_document = load_document
    calls = 0

    def load_and_mutate(path: str | Path, **kwargs: Any) -> Any:
        nonlocal calls
        document = original_load_document(path, **kwargs)
        calls += 1
        if calls == 3:
            camera_result.write_text("{}\n", encoding="utf-8")
        return document

    monkeypatch.setattr(
        "robotics_acceptance_harness.aggregate.load_document",
        load_and_mutate,
    )

    with pytest.raises(BundleValidationError, match="changed during aggregation"):
        aggregate_results(
            scenario_path=FIXTURES / "scenario.yaml",
            run_context_path=context_path,
            result_paths=[camera_result, control_result],
            output_path=tmp_path / "aggregate.json",
        )


def test_aggregate_rejects_transport_changed_after_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_qualification(tmp_path, relationship="link", consumer_link=2)
    scenario_path = tmp_path / "transport-scenario.yaml"
    context_path, results = transport_aggregate_inputs(tmp_path, scenario_path)
    qualification_path = tmp_path / "transport-qualification.json"
    original_load_document = load_document

    def load_and_mutate(path: str | Path, **kwargs: Any) -> Any:
        document = original_load_document(path, **kwargs)
        if Path(path).resolve() == qualification_path.resolve():
            qualification_path.write_text("{}\n", encoding="utf-8")
        return document

    monkeypatch.setattr(
        "robotics_acceptance_harness.aggregate.load_document",
        load_and_mutate,
    )

    with pytest.raises(BundleValidationError, match="changed during aggregation"):
        aggregate_results(
            scenario_path=scenario_path,
            run_context_path=context_path,
            result_paths=results,
            transport_qualification_path=qualification_path,
            output_path=tmp_path / "aggregate.json",
        )


def test_transport_qualification_proves_span_link(tmp_path: Path) -> None:
    result = transport_qualification(
        tmp_path,
        relationship="link",
        consumer_link=2,
    )

    assert result["schema_version"] == "transport-qualification-result.v1"
    assert result["verdict"]["status"] == "passed"
    assert result["causal_chains"][0]["root_trace_id"] == TRACE_ID
    assert result["causal_chains"][0]["hops"][0]["relationship"] == "link"
    assert result["channel_observations"][0]["status"] == "passed"
    assert "acceptance_run_sha256" not in result
    assert "per_domain_results" not in result
    channel = result["channel_contracts"][0]
    assert channel["source_domain_id"] == "camera-domain"
    assert channel["destination_domain_id"] == "control-domain"


def test_transport_qualification_binds_cross_domain_clock_relation(tmp_path: Path) -> None:
    scenario_path = transport_scenario(tmp_path)
    producer = trace_file(
        tmp_path,
        "camera-domain",
        "observation publish",
        2,
        message_id="message-1",
    )
    consumer = trace_file(
        tmp_path,
        "control-domain",
        "observation receive",
        3,
        message_id="message-1",
        link_byte=2,
    )
    channel_path = channel_contract(tmp_path, "link")
    relation = clock_relation(tmp_path, scenario_path, producer)

    output = evaluate_transport_qualification(
        run_id=RUN_ID,
        scenario_path=scenario_path,
        causal_chain_paths=(causal_chain(tmp_path, channel_path),),
        channel_contract_paths=(channel_path,),
        trace_paths={"camera-domain": producer, "control-domain": consumer},
        evidence_index_paths={
            "camera-domain": trace_evidence_index(tmp_path, "camera-domain", producer),
            "control-domain": trace_evidence_index(tmp_path, "control-domain", consumer),
        },
        clock_relation_paths=(relation,),
        observation_output_dir=tmp_path / "transport-observations",
        output_path=tmp_path / "transport-qualification-updated.json",
    )
    document = json.loads(output.read_text(encoding="utf-8"))

    assert document["schema_version"] == "transport-qualification-result.v1"
    assert document["clock_relations"][0]["status"] == "passed"


def test_transport_qualification_is_incomplete_without_clock_relation(
    tmp_path: Path,
) -> None:
    scenario_path = transport_scenario(tmp_path)
    producer = trace_file(tmp_path, "camera-domain", "observation publish", 2)
    consumer = trace_file(
        tmp_path,
        "control-domain",
        "observation receive",
        3,
        link_byte=2,
    )
    channel_path = channel_contract(tmp_path, "link")

    output = evaluate_transport_qualification(
        run_id=RUN_ID,
        scenario_path=scenario_path,
        causal_chain_paths=(causal_chain(tmp_path, channel_path),),
        channel_contract_paths=(channel_path,),
        trace_paths={"camera-domain": producer, "control-domain": consumer},
        evidence_index_paths={
            "camera-domain": trace_evidence_index(tmp_path, "camera-domain", producer),
            "control-domain": trace_evidence_index(tmp_path, "control-domain", consumer),
        },
        observation_output_dir=tmp_path / "transport-observations",
        output_path=tmp_path / "transport-qualification-updated.json",
    )
    document = json.loads(output.read_text(encoding="utf-8"))

    assert document["clock_relations"] == []
    assert document["verdict"]["status"] == "incomplete"


def test_transport_qualification_fails_measured_delivery_loss(tmp_path: Path) -> None:
    result = transport_qualification(
        tmp_path,
        relationship="link",
        consumer_link=2,
        consumer_message_id="message-2",
    )

    assert result["verdict"]["status"] == "failed"
    assert result["channel_observations"][0]["status"] == "failed"


@pytest.mark.parametrize(
    ("expected_status", "options"),
    [
        (
            "incomplete",
            {
                "producer_span_name": "unrelated producer",
                "consumer_span_name": "unrelated consumer",
            },
        ),
        (
            "error",
            {
                "producer_message_id": None,
                "consumer_message_id": None,
                "consumer_link": 2,
            },
        ),
    ],
)
def test_transport_qualification_emits_contract_valid_nonpassing_status(
    tmp_path: Path,
    expected_status: str,
    options: dict[str, Any],
) -> None:
    result = transport_qualification(tmp_path, relationship="link", **options)
    observation = json.loads(
        (tmp_path / "transport-observations" / "sensor.control.json").read_text(encoding="utf-8")
    )

    validate_document(observation)
    validate_document(result)
    assert observation["status"] == expected_status
    assert result["verdict"]["status"] == expected_status


def test_transport_qualification_proves_parent_chain(tmp_path: Path) -> None:
    result = transport_qualification(
        tmp_path,
        relationship="parent",
        consumer_parent=2,
    )

    assert result["verdict"]["status"] == "passed"
    assert result["causal_chains"][0]["hops"][0]["relationship"] == "parent"


def test_transport_qualification_evaluates_multiple_declared_chains(tmp_path: Path) -> None:
    result = transport_qualification(
        tmp_path,
        relationship="link",
        consumer_link=2,
        chain_count=2,
    )

    assert result["verdict"]["status"] == "passed"
    assert result["verdict"]["chain_count"] == 2
    assert result["verdict"]["passed_chain_count"] == 2
    assert len(result["causal_chain_contracts"]) == 2


def test_transport_qualification_rejects_span_identity_reused_across_domains(
    tmp_path: Path,
) -> None:
    with pytest.raises(TraceInputError, match="appears in domains"):
        transport_qualification(
            tmp_path,
            relationship="link",
            consumer_link=2,
            consumer_span_byte=2,
        )


def test_transport_qualification_fails_on_broken_span_link(tmp_path: Path) -> None:
    result = transport_qualification(
        tmp_path,
        relationship="link",
        consumer_link=4,
    )

    assert result["verdict"]["status"] == "failed"
    assert result["causal_chains"][0]["status"] == "failed"
    violations = result["causal_chains"][0]["violations"]
    assert violations[0]["code"] == "relationship_mismatch"


def test_transport_qualification_writes_contract_valid_failed_channel_observation(
    tmp_path: Path,
) -> None:
    result = transport_qualification(
        tmp_path,
        relationship="link",
        consumer_link=2,
        consumer_message_id="message-2",
    )

    assert result["verdict"]["status"] == "failed"
    observation = json.loads(
        (tmp_path / "transport-observations" / "sensor.control.json").read_text(encoding="utf-8")
    )
    assert observation["status"] == "failed"
    assert {item["code"] for item in observation["violations"]} == {
        "duplicate_count_exceeded",
        "loss_ratio_exceeded",
    }
    assert all("channel_id" not in item for item in observation["violations"])

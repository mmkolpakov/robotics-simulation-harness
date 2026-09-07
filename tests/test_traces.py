from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robotics_acceptance_harness.result import write_contract_json
from robotics_acceptance_harness.traces import (
    TraceInputError,
    TraceLink,
    TraceSpan,
    evaluate_causal_chain,
    evaluate_channel_delivery,
    load_otlp_json_traces,
)

SECOND_NS = 1_000_000_000


@pytest.mark.parametrize(
    "payload",
    [
        '{"unknownField": true}',
        '{"resourceSpans": "invalid"}',
        "null",
        "42",
        "[]",
        '""',
        "false",
        '{"resourceSpans": [[]]}',
        '{"resource_spans": [{"resource": ""}]}',
        '{"resourceSpans": [{"scopeSpans": [{"spans": [[]]}]}]}',
        '{"resourceSpans": [}',
    ],
)
def test_rejects_malformed_otlp_with_source_line(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text("{}\n" + payload + "\n", encoding="utf-8")

    with pytest.raises(TraceInputError, match=r"invalid\.jsonl:2:") as caught:
        load_otlp_json_traces(path, expected_run_id="run", expected_domain_id="domain")

    assert caught.value.__cause__ is not None


def channel_contract(**delivery_overrides: object) -> dict[str, Any]:
    delivery: dict[str, object] = {
        "observation_window_sec": 1,
        "minimum_source_messages": 1,
        "message_id_attribute": "messaging.message.id",
        "max_loss_ratio": 0,
        "max_duplicate_count": 0,
        "max_out_of_order_count": 0,
        "max_message_age_ms": 100,
    }
    delivery.update(delivery_overrides)
    return {
        "channel_id": "sensor.control",
        "source": {"domain_id": "source"},
        "destination": {"domain_id": "destination"},
        "delivery": delivery,
        "trace": {
            "producer_span_name": "publish",
            "consumer_span_name": "receive",
        },
    }


def span(
    *,
    domain_id: str,
    name: str,
    span_index: int,
    message_id: str | None,
    start_ns: int,
    duration_ns: int = 1_000,
    trace_id: str | None = None,
    parent_span_id: str = "",
    links: tuple[TraceLink, ...] = (),
) -> TraceSpan:
    return TraceSpan(
        trace_id=trace_id or f"{span_index + 1:032x}",
        span_id=f"{span_index + 1:016x}",
        parent_span_id=parent_span_id,
        name=name,
        run_id="run-01234567-89ab-4def-8123-456789abcdef",
        domain_id=domain_id,
        message_id=message_id,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=start_ns + duration_ns,
        links=links,
    )


def duplicated_delivery_spans() -> dict[str, list[TraceSpan]]:
    producer = span(
        domain_id="source",
        name="publish",
        span_index=1,
        message_id="message-1",
        start_ns=10 * SECOND_NS,
    )
    consumers = [
        span(
            domain_id="destination",
            name="receive",
            span_index=index,
            message_id="message-1",
            start_ns=10 * SECOND_NS + index * 10_000,
        )
        for index in (2, 3)
    ]
    return {"source": [producer], "destination": consumers}


def test_duplicate_producer_ids_fail_closed_without_losing_counts() -> None:
    producers = [
        span(
            domain_id="source",
            name="publish",
            span_index=index,
            message_id="reused-message",
            start_ns=10 * SECOND_NS + index * 1_000,
        )
        for index in range(20)
    ]
    consumer = span(
        domain_id="destination",
        name="receive",
        span_index=100,
        message_id="reused-message",
        start_ns=10 * SECOND_NS + 100_000,
    )

    observation = evaluate_channel_delivery(
        channel_contract(minimum_source_messages=20),
        {"source": producers, "destination": [consumer]},
    )

    assert observation.status == "error"
    assert observation.sent_count == 20
    assert observation.received_count == 1
    assert observation.lost_count == 19
    assert observation.loss_ratio == pytest.approx(0.95)
    assert {violation.code for violation in observation.violations} >= {
        "ambiguous_message_id",
        "loss_ratio_exceeded",
    }


def test_emitted_channel_errors_serialize_through_the_public_contract(tmp_path: Path) -> None:
    producers = [
        span(
            domain_id="source",
            name="publish",
            span_index=index,
            message_id="reused-message",
            start_ns=10 * SECOND_NS + index * 1_000,
        )
        for index in range(2)
    ]
    consumer = span(
        domain_id="destination",
        name="receive",
        span_index=100,
        message_id="reused-message",
        start_ns=10 * SECOND_NS + 100_000,
    )
    observation = evaluate_channel_delivery(
        channel_contract(minimum_source_messages=2),
        {"source": producers, "destination": [consumer]},
    )
    document = {
        "schema_version": "transport-channel-observation.v1",
        "observation_id": "observation-00000000-0000-4000-8000-000000000001",
        "run_id": "run-00000000-0000-4000-8000-000000000001",
        "channel_id": "sensor.control",
        "channel_contract_sha256": "f" * 64,
        "started_at": "2026-07-26T12:00:00Z",
        "finished_at": "2026-07-26T12:00:01Z",
        "sent_count": observation.sent_count,
        "received_count": observation.received_count,
        "lost_count": observation.lost_count,
        "duplicate_count": observation.duplicate_count,
        "out_of_order_count": observation.out_of_order_count,
        "loss_ratio": observation.loss_ratio,
        "max_message_age_ms": observation.max_message_age_ms,
        "status": observation.status,
        "violations": [
            {"code": violation.code, "message": violation.message}
            for violation in observation.violations
        ],
    }

    output = write_contract_json(document, tmp_path / "observation.json")

    assert output.is_file()


def test_duplicate_consumers_are_counted_against_delivery_policy() -> None:
    observation = evaluate_channel_delivery(
        channel_contract(),
        duplicated_delivery_spans(),
    )

    assert observation.status == "failed"
    assert observation.sent_count == 1
    assert observation.received_count == 2
    assert observation.lost_count == 0
    assert observation.duplicate_count == 1
    assert [violation.code for violation in observation.violations] == ["duplicate_count_exceeded"]


def test_known_delivery_failure_has_priority_over_insufficient_messages() -> None:
    observation = evaluate_channel_delivery(
        channel_contract(minimum_source_messages=2),
        duplicated_delivery_spans(),
    )

    assert observation.status == "failed"
    assert {violation.code for violation in observation.violations} == {
        "duplicate_count_exceeded",
        "insufficient_messages",
    }


def test_channel_observation_uses_declared_window() -> None:
    producer = span(
        domain_id="source",
        name="publish",
        span_index=1,
        message_id="message-1",
        start_ns=10 * SECOND_NS,
    )
    consumer = span(
        domain_id="destination",
        name="receive",
        span_index=2,
        message_id="message-1",
        start_ns=10 * SECOND_NS + 50_000_000,
    )

    observation = evaluate_channel_delivery(
        channel_contract(),
        {"source": [producer], "destination": [consumer]},
    )

    assert observation.status == "passed"
    assert observation.started_at_ns == 10 * SECOND_NS
    assert observation.finished_at_ns == 11 * SECOND_NS
    assert observation.sent_count == observation.received_count == 1


def test_channel_span_outside_declared_window_fails_closed() -> None:
    producer = span(
        domain_id="source",
        name="publish",
        span_index=1,
        message_id="message-1",
        start_ns=10 * SECOND_NS,
    )
    late_consumer = span(
        domain_id="destination",
        name="receive",
        span_index=2,
        message_id="message-1",
        start_ns=11 * SECOND_NS + 1,
    )

    observation = evaluate_channel_delivery(
        channel_contract(),
        {"source": [producer], "destination": [late_consumer]},
    )

    assert observation.status == "error"
    assert observation.sent_count == 1
    assert observation.received_count == 0
    assert observation.lost_count == 1
    assert observation.loss_ratio == 1
    assert {violation.code for violation in observation.violations} >= {
        "observation_window_exceeded",
        "loss_ratio_exceeded",
    }


def test_invalid_message_ids_still_emit_balanced_counters() -> None:
    producer = span(
        domain_id="source",
        name="publish",
        span_index=1,
        message_id=None,
        start_ns=10 * SECOND_NS,
    )
    consumer = span(
        domain_id="destination",
        name="receive",
        span_index=2,
        message_id=None,
        start_ns=10 * SECOND_NS + 50_000_000,
    )

    observation = evaluate_channel_delivery(
        channel_contract(),
        {"source": [producer], "destination": [consumer]},
    )

    assert observation.status == "error"
    assert observation.received_count == (
        observation.sent_count - observation.lost_count + observation.duplicate_count
    )


def _causal_contracts() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    channels = [
        {
            "channel_id": "sensor.decision",
            "source": {"domain_id": "sensor"},
            "destination": {"domain_id": "decision"},
            "trace": {
                "producer_span_name": "sensor publish",
                "consumer_span_name": "decision receive",
                "relationship": "link",
            },
        },
        {
            "channel_id": "decision.actuation",
            "source": {"domain_id": "decision"},
            "destination": {"domain_id": "actuation"},
            "trace": {
                "producer_span_name": "decision publish",
                "consumer_span_name": "actuation receive",
                "relationship": "parent",
            },
        },
    ]
    chain = {
        "channel_contracts": [
            {"channel_id": channel["channel_id"], "sha256": "f" * 64} for channel in channels
        ]
    }
    return chain, channels


def _causal_spans(*, reverse_middle_edge: bool) -> dict[str, list[TraceSpan]]:
    trace_id = "1" * 32
    producer_one = span(
        domain_id="sensor",
        name="sensor publish",
        span_index=0,
        message_id="message-1",
        start_ns=10 * SECOND_NS,
        trace_id=trace_id,
    )
    producer_two = span(
        domain_id="decision",
        name="decision publish",
        span_index=2,
        message_id="message-2",
        start_ns=10 * SECOND_NS + 20_000,
        trace_id=trace_id,
        parent_span_id="" if reverse_middle_edge else f"{2:016x}",
    )
    consumer_one = span(
        domain_id="decision",
        name="decision receive",
        span_index=1,
        message_id="message-1",
        start_ns=10 * SECOND_NS + 10_000,
        trace_id=trace_id,
        parent_span_id=f"{3:016x}" if reverse_middle_edge else "",
        links=(TraceLink(trace_id, f"{1:016x}", "message-1"),),
    )
    consumer_two = span(
        domain_id="actuation",
        name="actuation receive",
        span_index=3,
        message_id="message-2",
        start_ns=10 * SECOND_NS + 30_000,
        trace_id=trace_id,
        parent_span_id=f"{3:016x}",
    )
    return {
        "sensor": [producer_one],
        "decision": [consumer_one, producer_two],
        "actuation": [consumer_two],
    }


def test_causal_chain_requires_forward_reachability_between_channels() -> None:
    chain, channels = _causal_contracts()

    valid = evaluate_causal_chain(
        chain,
        channels,
        _causal_spans(reverse_middle_edge=False),
    )
    reversed_edge = evaluate_causal_chain(
        chain,
        channels,
        _causal_spans(reverse_middle_edge=True),
    )

    assert valid.status == "passed"
    assert reversed_edge.status == "failed"
    assert [violation.code for violation in reversed_edge.violations] == ["relationship_mismatch"]


def test_causal_chain_rejects_consumer_before_producer() -> None:
    chain, channels = _causal_contracts()
    spans = _causal_spans(reverse_middle_edge=False)
    producer = spans["sensor"][0]
    spans["decision"][0] = span(
        domain_id="decision",
        name="decision receive",
        span_index=1,
        message_id="message-1",
        start_ns=producer.start_time_unix_nano - 1,
        trace_id=producer.trace_id,
        links=(TraceLink(producer.trace_id, producer.span_id, "message-1"),),
    )

    evaluation = evaluate_causal_chain(chain, channels, spans)

    assert evaluation.status == "failed"
    assert evaluation.violations[0].code == "temporal_order_mismatch"


def test_causal_chain_rejects_next_hop_before_previous_consumer() -> None:
    chain, channels = _causal_contracts()
    spans = _causal_spans(reverse_middle_edge=False)
    previous_consumer = spans["decision"][0]
    spans["decision"][1] = span(
        domain_id="decision",
        name="decision publish",
        span_index=2,
        message_id="message-2",
        start_ns=previous_consumer.start_time_unix_nano - 1,
        trace_id=previous_consumer.trace_id,
        parent_span_id=previous_consumer.span_id,
    )

    evaluation = evaluate_causal_chain(chain, channels, spans)

    assert evaluation.status == "failed"
    assert any(violation.code == "temporal_order_mismatch" for violation in evaluation.violations)


def test_known_causal_failure_is_not_masked_by_another_missing_channel() -> None:
    chain, channels = _causal_contracts()
    spans = _causal_spans(reverse_middle_edge=False)
    spans["decision"] = [spans["decision"][0]]
    spans["actuation"] = []
    spans["decision"][0] = span(
        domain_id="decision",
        name="decision receive",
        span_index=1,
        message_id="message-1",
        start_ns=10 * SECOND_NS + 10_000,
        trace_id="1" * 32,
    )

    evaluation = evaluate_causal_chain(chain, channels, spans)

    assert evaluation.status == "failed"
    assert {violation.code for violation in evaluation.violations} == {
        "relationship_mismatch",
        "missing_span",
    }


def test_otlp_traces_are_parsed_only_when_digest_matches(tmp_path: Path) -> None:
    path = tmp_path / "traces.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(TraceInputError, match="digest differs"):
        load_otlp_json_traces(
            path,
            expected_run_id="run-00000000-0000-4000-8000-000000000001",
            expected_domain_id="sensor",
            expected_sha256="0" * 64,
        )

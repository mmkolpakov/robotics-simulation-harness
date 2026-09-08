from __future__ import annotations

import base64
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from google.protobuf.json_format import ParseError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from robotics_runtime_contracts import channel_observation_status, derive_channel_violations

from robotics_acceptance_harness.otel import (
    otlp_attributes,
    parse_otlp_request,
    read_otlp_json_lines,
)

MESSAGE_ID_ATTRIBUTE = "messaging.message.id"
_OTLP_IDENTIFIER_LENGTHS = {
    "traceId": 16,
    "spanId": 8,
    "parentSpanId": 8,
}


class TraceInputError(ValueError):
    """Raised when OTLP trace evidence is incomplete or contradictory."""


@dataclass(frozen=True, slots=True)
class TraceLink:
    trace_id: str
    span_id: str
    message_id: str | None


@dataclass(frozen=True, slots=True)
class TraceSpan:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    run_id: str
    domain_id: str
    message_id: str | None
    start_time_unix_nano: int
    end_time_unix_nano: int
    links: tuple[TraceLink, ...]


@dataclass(frozen=True, slots=True)
class ChainViolation:
    code: str
    message: str
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class CausalHop:
    channel_id: str
    relationship: Literal["link", "parent"]
    producer: TraceSpan
    consumer: TraceSpan


@dataclass(frozen=True, slots=True)
class CausalChainEvaluation:
    status: Literal["passed", "failed", "incomplete", "error"]
    root_trace_id: str | None
    trace_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    hops: tuple[CausalHop, ...]
    violations: tuple[ChainViolation, ...]


@dataclass(frozen=True, slots=True)
class ChannelObservationEvaluation:
    status: Literal["passed", "failed", "incomplete", "error"]
    started_at_ns: int
    finished_at_ns: int
    sent_count: int
    received_count: int
    lost_count: int
    duplicate_count: int
    out_of_order_count: int
    loss_ratio: float
    max_message_age_ms: float
    violations: tuple[ChainViolation, ...]


def _protobuf_identifier(value: Any, *, field: str, path: str) -> str:
    if not isinstance(value, str):
        raise TraceInputError(f"{path}.{field} must be a hexadecimal string")
    expected_bytes = _OTLP_IDENTIFIER_LENGTHS[field]
    if value == "" and field == "parentSpanId":
        return ""
    try:
        decoded = bytes.fromhex(value)
    except ValueError as error:
        raise TraceInputError(f"{path}.{field} is not hexadecimal") from error
    if len(decoded) != expected_bytes:
        raise TraceInputError(f"{path}.{field} must encode exactly {expected_bytes} bytes")
    return base64.b64encode(decoded).decode("ascii")


def _normalize_otlp_json(value: Any, *, path: str = "$") -> Any:
    """Adapt the OTLP hex identifier exception to protobuf's JSON parser."""

    if isinstance(value, list):
        return [
            _normalize_otlp_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        item_path = f"{path}.{key}"
        if key in _OTLP_IDENTIFIER_LENGTHS:
            normalized[key] = _protobuf_identifier(item, field=key, path=path)
        else:
            normalized[key] = _normalize_otlp_json(item, path=item_path)
    return normalized


def _string_attribute(
    attributes: Mapping[str, Any],
    name: str,
    *,
    required: bool,
    location: str,
) -> str | None:
    value = attributes.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise TraceInputError(f"{location} requires non-empty string attribute {name!r}")
    return value


def load_otlp_json_traces(
    path: str | Path,
    *,
    expected_run_id: str,
    expected_domain_id: str,
    expected_sha256: str | None = None,
) -> tuple[TraceSpan, ...]:
    """Read official newline-delimited OTLP/JSON Collector trace output."""

    source, lines = read_otlp_json_lines(path, expected_sha256, TraceInputError)

    spans: list[TraceSpan] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            request = parse_otlp_request(
                _normalize_otlp_json(payload),
                ExportTraceServiceRequest(),
            )
        except (ParseError, TypeError, ValueError) as error:
            raise TraceInputError(
                f"invalid OTLP JSON at {source}:{line_number}: {error}"
            ) from error
        for resource_spans in request.resource_spans:
            resource_attributes = otlp_attributes(resource_spans.resource.attributes)
            for scope_spans in resource_spans.scope_spans:
                scope_attributes = {
                    **resource_attributes,
                    **otlp_attributes(scope_spans.scope.attributes),
                }
                for span in scope_spans.spans:
                    location = f"{source}:{line_number} span {span.name!r}"
                    if (
                        span.dropped_attributes_count
                        or span.dropped_events_count
                        or span.dropped_links_count
                    ):
                        raise TraceInputError(
                            f"{location} reports dropped trace data and cannot prove causality"
                        )
                    attributes = {**scope_attributes, **otlp_attributes(span.attributes)}
                    run_id = _string_attribute(
                        attributes,
                        "run.id",
                        required=True,
                        location=location,
                    )
                    domain_id = _string_attribute(
                        attributes,
                        "domain.id",
                        required=True,
                        location=location,
                    )
                    if run_id != expected_run_id or domain_id != expected_domain_id:
                        raise TraceInputError(
                            f"{location} is bound to run={run_id!r}, domain={domain_id!r}"
                        )
                    trace_id = bytes(span.trace_id).hex()
                    span_id = bytes(span.span_id).hex()
                    parent_span_id = bytes(span.parent_span_id).hex()
                    if len(trace_id) != 32 or trace_id == "0" * 32:
                        raise TraceInputError(f"{location} contains an invalid traceId")
                    if len(span_id) != 16 or span_id == "0" * 16:
                        raise TraceInputError(f"{location} contains an invalid spanId")
                    if span.end_time_unix_nano < span.start_time_unix_nano:
                        raise TraceInputError(f"{location} ends before it starts")
                    identity = (trace_id, span_id)
                    if identity in seen:
                        raise TraceInputError(f"{location} repeats span {span_id}")
                    seen.add(identity)

                    links: list[TraceLink] = []
                    for link in span.links:
                        link_trace_id = bytes(link.trace_id).hex()
                        link_span_id = bytes(link.span_id).hex()
                        if (
                            len(link_trace_id) != 32
                            or link_trace_id == "0" * 32
                            or len(link_span_id) != 16
                            or link_span_id == "0" * 16
                        ):
                            raise TraceInputError(f"{location} contains an invalid span link")
                        link_attributes = otlp_attributes(link.attributes)
                        links.append(
                            TraceLink(
                                trace_id=link_trace_id,
                                span_id=link_span_id,
                                message_id=_string_attribute(
                                    link_attributes,
                                    MESSAGE_ID_ATTRIBUTE,
                                    required=False,
                                    location=f"{location} link",
                                ),
                            )
                        )
                    spans.append(
                        TraceSpan(
                            trace_id=trace_id,
                            span_id=span_id,
                            parent_span_id=parent_span_id,
                            name=str(span.name),
                            run_id=run_id,
                            domain_id=domain_id,
                            message_id=_string_attribute(
                                attributes,
                                MESSAGE_ID_ATTRIBUTE,
                                required=False,
                                location=location,
                            ),
                            start_time_unix_nano=int(span.start_time_unix_nano),
                            end_time_unix_nano=int(span.end_time_unix_nano),
                            links=tuple(links),
                        )
                    )
    if not spans:
        raise TraceInputError(f"{source} contains no spans")
    return tuple(spans)


def _channel_spans(
    contract: Mapping[str, Any],
    spans_by_domain: Mapping[str, Sequence[TraceSpan]],
) -> tuple[list[TraceSpan], list[TraceSpan]]:
    source_domain = str(contract["source"]["domain_id"])
    destination_domain = str(contract["destination"]["domain_id"])
    producer_name = str(contract["trace"]["producer_span_name"])
    consumer_name = str(contract["trace"]["consumer_span_name"])
    producers = [
        span for span in spans_by_domain.get(source_domain, ()) if span.name == producer_name
    ]
    consumers = [
        span for span in spans_by_domain.get(destination_domain, ()) if span.name == consumer_name
    ]
    return producers, consumers


def _relationship_matches(
    relationship: str,
    producer: TraceSpan,
    consumer: TraceSpan,
) -> bool:
    if relationship == "parent":
        return (
            consumer.trace_id == producer.trace_id and consumer.parent_span_id == producer.span_id
        )
    return any(
        link.trace_id == producer.trace_id
        and link.span_id == producer.span_id
        and (link.message_id is None or link.message_id == producer.message_id)
        for link in consumer.links
    )


def _trace_graph(
    spans_by_domain: Mapping[str, Sequence[TraceSpan]],
) -> Mapping[tuple[str, str], set[tuple[str, str]]]:
    graph: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    known_spans = {
        (span.trace_id, span.span_id): span for spans in spans_by_domain.values() for span in spans
    }
    for spans in spans_by_domain.values():
        for span in spans:
            current = (span.trace_id, span.span_id)
            graph[current]
            if span.parent_span_id:
                parent = (span.trace_id, span.parent_span_id)
                parent_span = known_spans.get(parent)
                if (
                    parent_span is not None
                    and span.start_time_unix_nano >= parent_span.start_time_unix_nano
                ):
                    graph[parent].add(current)
            for link in span.links:
                target = (link.trace_id, link.span_id)
                target_span = known_spans.get(target)
                if (
                    target_span is not None
                    and span.start_time_unix_nano >= target_span.start_time_unix_nano
                ):
                    graph[target].add(current)
    return graph


def validate_trace_set(
    spans_by_domain: Mapping[str, Sequence[TraceSpan]],
) -> None:
    """Reject a span identity that appears in more than one domain evidence file."""

    identities: dict[tuple[str, str], str] = {}
    for domain_id, spans in spans_by_domain.items():
        for span in spans:
            identity = (span.trace_id, span.span_id)
            previous_domain = identities.get(identity)
            if previous_domain is not None:
                raise TraceInputError(
                    "span identity "
                    f"{span.trace_id}/{span.span_id} appears in domains "
                    f"{previous_domain!r} and {domain_id!r}"
                )
            identities[identity] = domain_id


def _reachable(
    graph: Mapping[tuple[str, str], set[tuple[str, str]]],
    source: tuple[str, str],
    target: tuple[str, str],
) -> bool:
    pending = [source]
    visited: set[tuple[str, str]] = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node in visited:
            continue
        visited.add(node)
        pending.extend(graph.get(node, ()))
    return False


def evaluate_causal_chain(
    chain_contract: Mapping[str, Any],
    channel_contracts: Sequence[Mapping[str, Any]],
    spans_by_domain: Mapping[str, Sequence[TraceSpan]],
) -> CausalChainEvaluation:
    """Verify the complete expected channel sequence as one connected trace graph."""

    validate_trace_set(spans_by_domain)
    expected_channel_ids = tuple(
        str(item["channel_id"]) for item in chain_contract["channel_contracts"]
    )
    observed_channel_ids = tuple(str(item["channel_id"]) for item in channel_contracts)
    if observed_channel_ids != expected_channel_ids:
        raise TraceInputError("channel contracts differ from the causal-chain contract")

    violations: list[ChainViolation] = []
    hops: list[CausalHop] = []
    missing = False
    for contract in channel_contracts:
        channel_id = str(contract["channel_id"])
        producers, consumers = _channel_spans(contract, spans_by_domain)
        if not producers:
            missing = True
            violations.append(
                ChainViolation(
                    code="missing_span",
                    channel_id=channel_id,
                    message="producer span is absent",
                )
            )
            continue
        if not consumers:
            missing = True
            violations.append(
                ChainViolation(
                    code="missing_span",
                    channel_id=channel_id,
                    message="consumer span is absent",
                )
            )
            continue

        message_pairs = [
            (producer, consumer)
            for producer in producers
            for consumer in consumers
            if producer.message_id is not None and producer.message_id == consumer.message_id
        ]
        if not message_pairs:
            violations.append(
                ChainViolation(
                    code="message_id_mismatch",
                    channel_id=channel_id,
                    message="producer and consumer have no common messaging.message.id",
                )
            )
            continue
        relationship = cast(
            Literal["link", "parent"],
            str(contract["trace"]["relationship"]),
        )
        relationship_pairs = [
            pair for pair in message_pairs if _relationship_matches(relationship, pair[0], pair[1])
        ]
        if not relationship_pairs:
            violations.append(
                ChainViolation(
                    code="relationship_mismatch",
                    channel_id=channel_id,
                    message=f"no producer-consumer pair satisfies {relationship!r}",
                )
            )
            continue
        valid_pairs = [
            pair
            for pair in relationship_pairs
            if pair[1].start_time_unix_nano >= pair[0].start_time_unix_nano
        ]
        if not valid_pairs:
            violations.append(
                ChainViolation(
                    code="temporal_order_mismatch",
                    channel_id=channel_id,
                    message="consumer span starts before its producer span",
                )
            )
            continue
        producer, consumer = min(
            valid_pairs,
            key=lambda pair: (
                pair[1].start_time_unix_nano,
                pair[0].start_time_unix_nano,
                pair[0].span_id,
                pair[1].span_id,
            ),
        )
        hops.append(
            CausalHop(
                channel_id=channel_id,
                relationship=relationship,
                producer=producer,
                consumer=consumer,
            )
        )

    if len(hops) == len(channel_contracts):
        graph = _trace_graph(spans_by_domain)
        for previous, current in zip(hops, hops[1:], strict=False):
            previous_consumer = (
                previous.consumer.trace_id,
                previous.consumer.span_id,
            )
            current_producer = (
                current.producer.trace_id,
                current.producer.span_id,
            )
            if current.producer.start_time_unix_nano < previous.consumer.start_time_unix_nano:
                violations.append(
                    ChainViolation(
                        code="temporal_order_mismatch",
                        channel_id=current.channel_id,
                        message=(
                            "the next channel producer starts before the preceding consumer span"
                        ),
                    )
                )
            elif previous.consumer.domain_id != current.producer.domain_id or not _reachable(
                graph, previous_consumer, current_producer
            ):
                violations.append(
                    ChainViolation(
                        code="relationship_mismatch",
                        channel_id=current.channel_id,
                        message=(
                            "the preceding consumer does not causally reach "
                            "the next channel producer"
                        ),
                    )
                )

    status: Literal["passed", "failed", "incomplete", "error"]
    if not violations:
        status = "passed"
    elif any(violation.code != "missing_span" for violation in violations):
        status = "failed"
    elif missing:
        status = "incomplete"
    else:
        status = "error"
    trace_ids = tuple(
        sorted({span.trace_id for hop in hops for span in (hop.producer, hop.consumer)})
    )
    return CausalChainEvaluation(
        status=status,
        root_trace_id=hops[0].producer.trace_id if hops else None,
        trace_ids=trace_ids,
        channel_ids=expected_channel_ids,
        hops=tuple(hops),
        violations=tuple(violations),
    )


def evaluate_channel_delivery(
    contract: Mapping[str, Any],
    spans_by_domain: Mapping[str, Sequence[TraceSpan]],
) -> ChannelObservationEvaluation:
    """Measure one channel from message-correlated producer and consumer spans."""

    channel_id = str(contract["channel_id"])
    producers, consumers = _channel_spans(contract, spans_by_domain)
    all_spans = [*producers, *consumers]
    if not all_spans:
        return ChannelObservationEvaluation(
            status="incomplete",
            started_at_ns=0,
            finished_at_ns=0,
            sent_count=0,
            received_count=0,
            lost_count=0,
            duplicate_count=0,
            out_of_order_count=0,
            loss_ratio=0.0,
            max_message_age_ms=0.0,
            violations=(
                ChainViolation(
                    code="insufficient_messages",
                    channel_id=channel_id,
                    message="no producer or consumer spans were observed",
                ),
            ),
        )

    delivery = contract["delivery"]
    window_duration_ns = int(float(delivery["observation_window_sec"]) * 1_000_000_000)
    window_start_ns = (
        min(span.start_time_unix_nano for span in producers)
        if producers
        else min(span.start_time_unix_nano for span in all_spans)
    )
    window_end_ns = window_start_ns + window_duration_ns
    outside_window = [
        span
        for span in all_spans
        if span.start_time_unix_nano < window_start_ns or span.end_time_unix_nano > window_end_ns
    ]
    producers = [
        span
        for span in producers
        if span.start_time_unix_nano >= window_start_ns and span.end_time_unix_nano <= window_end_ns
    ]
    consumers = [
        span
        for span in consumers
        if span.start_time_unix_nano >= window_start_ns and span.end_time_unix_nano <= window_end_ns
    ]

    if any(span.message_id is None for span in [*producers, *consumers]):
        lost_count = len(producers)
        duplicate_count = len(consumers)
        return ChannelObservationEvaluation(
            status="error",
            started_at_ns=window_start_ns,
            finished_at_ns=window_end_ns,
            sent_count=len(producers),
            received_count=len(consumers),
            lost_count=lost_count,
            duplicate_count=duplicate_count,
            out_of_order_count=0,
            loss_ratio=lost_count / len(producers) if producers else 0.0,
            max_message_age_ms=0.0,
            violations=(
                ChainViolation(
                    code="invalid_observation",
                    channel_id=channel_id,
                    message=f"every span must carry {MESSAGE_ID_ATTRIBUTE}",
                ),
            ),
        )

    producer_groups: dict[str, list[TraceSpan]] = defaultdict(list)
    for producer in producers:
        producer_groups[str(producer.message_id)].append(producer)
    consumer_groups: dict[str, list[TraceSpan]] = defaultdict(list)
    for consumer in consumers:
        consumer_groups[str(consumer.message_id)].append(consumer)
    lost_count = sum(
        max(0, len(items) - len(consumer_groups.get(message_id, ())))
        for message_id, items in producer_groups.items()
    )
    duplicate_count = sum(
        max(0, len(items) - len(producer_groups.get(message_id, ())))
        for message_id, items in consumer_groups.items()
    )
    loss_ratio = lost_count / len(producers) if producers else 0.0

    ordered_producers = sorted(
        producers,
        key=lambda span: (
            span.start_time_unix_nano,
            str(span.message_id),
            span.trace_id,
            span.span_id,
        ),
    )
    producer_order = {
        (span.trace_id, span.span_id): index for index, span in enumerate(ordered_producers)
    }
    matched: list[tuple[TraceSpan, TraceSpan]] = []
    for message_id, producer_items in producer_groups.items():
        consumer_items = consumer_groups.get(message_id, ())
        matched.extend(
            zip(
                sorted(
                    producer_items,
                    key=lambda span: (
                        span.start_time_unix_nano,
                        span.trace_id,
                        span.span_id,
                    ),
                ),
                sorted(
                    consumer_items,
                    key=lambda span: (
                        span.start_time_unix_nano,
                        span.trace_id,
                        span.span_id,
                    ),
                ),
                strict=False,
            )
        )
    received_order = [
        producer_order[(producer.trace_id, producer.span_id)]
        for producer, consumer in sorted(
            matched,
            key=lambda pair: (
                pair[1].start_time_unix_nano,
                str(pair[1].message_id),
                pair[1].trace_id,
                pair[1].span_id,
            ),
        )
    ]
    highest_seen = -1
    out_of_order_count = 0
    for index in received_order:
        if index < highest_seen:
            out_of_order_count += 1
        highest_seen = max(highest_seen, index)

    message_ages = [
        (consumer.start_time_unix_nano - producer.end_time_unix_nano) / 1_000_000
        for producer, consumer in matched
    ]
    violations: list[ChainViolation] = []
    if outside_window:
        violations.append(
            ChainViolation(
                code="observation_window_exceeded",
                channel_id=channel_id,
                message=(
                    f"{len(outside_window)} channel spans fall outside the declared "
                    f"{delivery['observation_window_sec']} second observation window"
                ),
            )
        )
    ambiguous_ids = sorted(
        message_id for message_id, items in producer_groups.items() if len(items) > 1
    )
    if ambiguous_ids:
        violations.append(
            ChainViolation(
                code="ambiguous_message_id",
                channel_id=channel_id,
                message=(f"producer message identifiers are not unique: {ambiguous_ids}"),
            )
        )
    if any(age < 0 for age in message_ages):
        violations.append(
            ChainViolation(
                code="invalid_observation",
                channel_id=channel_id,
                message="consumer timestamp precedes producer completion",
            )
        )
    max_message_age_ms = max(message_ages, default=0.0)
    existing_codes = {item.code for item in violations}
    derived_codes = derive_channel_violations(
        delivery,
        sent_count=len(producers),
        loss_ratio=loss_ratio,
        duplicate_count=duplicate_count,
        out_of_order_count=out_of_order_count,
        max_message_age_ms=max_message_age_ms,
        observation_duration_sec=(window_end_ns - window_start_ns) / 1_000_000_000,
        reported_violation_codes=existing_codes,
    )
    messages = {
        "insufficient_messages": (
            f"observed {len(producers)} source messages; "
            f"requires {delivery['minimum_source_messages']}"
        ),
        "loss_ratio_exceeded": f"loss ratio {loss_ratio} exceeds {delivery['max_loss_ratio']}",
        "duplicate_count_exceeded": (
            f"duplicate count {duplicate_count} exceeds {delivery['max_duplicate_count']}"
        ),
        "out_of_order_count_exceeded": (
            f"out-of-order count {out_of_order_count} exceeds {delivery['max_out_of_order_count']}"
        ),
        "message_age_exceeded": (
            f"message age {max_message_age_ms} ms exceeds {delivery['max_message_age_ms']} ms"
        ),
    }
    violations.extend(
        ChainViolation(code=code, channel_id=channel_id, message=messages[code])
        for code in sorted(derived_codes - existing_codes)
    )
    status = channel_observation_status(item.code for item in violations)
    return ChannelObservationEvaluation(
        status=status,
        started_at_ns=window_start_ns,
        finished_at_ns=window_end_ns,
        sent_count=len(producers),
        received_count=len(consumers),
        lost_count=lost_count,
        duplicate_count=duplicate_count,
        out_of_order_count=out_of_order_count,
        loss_ratio=loss_ratio,
        max_message_age_ms=max_message_age_ms,
        violations=tuple(violations),
    )

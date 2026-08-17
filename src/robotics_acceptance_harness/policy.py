from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from robotics_acceptance_harness.evidence import VerifiedEvidence
from robotics_acceptance_harness.metrics import (
    AssertionEvaluation,
    HistogramSample,
    MetricAggregationError,
    MetricPoint,
    counter_window_aggregate,
    evaluate_metric_assertions,
    histogram_window_coverage,
    require_window_coverage,
)

MESSAGE_COUNTER_UNIT = "{message}"
SEQUENCE_METHOD_ATTRIBUTE = "sequence.measurement.method"
SINGLE_PUBLISHER_SEQUENCE_METHOD = "rmw_publication_sequence_single_publisher"


def _boolean_evaluation(assertion_id: str, passed: bool, message: str) -> AssertionEvaluation:
    return AssertionEvaluation(
        assertion_id=assertion_id,
        status="passed" if passed else "failed",
        observed_value=1 if passed else 0,
        unit="1",
        message="" if passed else message,
    )


def evaluate_data_plane_policy(
    policy: Mapping[str, Any],
    runtime: Mapping[str, Any],
    samples: Sequence[MetricPoint],
    *,
    domain_id: str,
    window_start_ns: int,
    window_end_ns: int,
) -> tuple[AssertionEvaluation, ...]:
    """Evaluate static transport facts and attributed data-plane telemetry."""

    observed = runtime["data_plane"]
    evaluations = [
        _boolean_evaluation(
            f"data-plane-{field.replace('_', '-')}",
            observed.get(field) == policy[field],
            f"expected {field}={policy[field]!r}; observed {observed.get(field)!r}",
        )
        for field in ("shm_transport", "data_sharing", "private_ipc")
    ]
    if "middleware_configuration_sha256" in policy:
        evaluations.append(
            _boolean_evaluation(
                "data-plane-middleware-configuration",
                observed.get("middleware_configuration_sha256")
                == policy["middleware_configuration_sha256"],
                "middleware configuration digest differs",
            )
        )
    try:
        measured_names = {
            "robotics.message.age",
            "robotics.message.received",
            "robotics.message.lost",
            "robotics.message.sequence_error",
        }
        channels = {
            sample.attributes.get("channel")
            for sample in samples
            if sample.name in measured_names and sample.attributes.get("domain.id") == domain_id
        }
        if len(channels) != 1 or not isinstance(next(iter(channels), None), str):
            raise MetricAggregationError(
                "data-plane evidence must identify exactly one measured channel"
            )
        channel = str(next(iter(channels)))
        attribute_match = {"domain.id": domain_id, "channel": channel}
        counter_attribute_match = {
            **attribute_match,
            SEQUENCE_METHOD_ATTRIBUTE: SINGLE_PUBLISHER_SEQUENCE_METHOD,
        }
        message_age_points = [
            sample
            for sample in samples
            if sample.name == "robotics.message.age"
            and all(sample.attributes.get(key) == value for key, value in attribute_match.items())
        ]
        if not message_age_points or not all(
            isinstance(sample, HistogramSample) for sample in message_age_points
        ):
            raise MetricAggregationError("robotics.message.age must be an OTLP Histogram")
        message_age_coverage = histogram_window_coverage(
            [sample for sample in message_age_points if isinstance(sample, HistogramSample)],
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        require_window_coverage(
            message_age_coverage,
            metric_name="robotics.message.age",
            temporality=message_age_points[0].temporality,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        evaluations.extend(
            evaluate_metric_assertions(
                (
                    {
                        "assertion_id": "data-plane-message-age",
                        "metric_name": "robotics.message.age",
                        "unit": "ms",
                        "aggregation": "p95",
                        "operator": "lte",
                        "threshold": policy["max_message_age_ms"],
                        "window_sec": 86_400,
                        "attribute_match": attribute_match,
                    },
                ),
                samples,
                window_start_ns=window_start_ns,
                window_end_ns=window_end_ns,
            )
        )
        received = counter_window_aggregate(
            samples,
            "robotics.message.received",
            attribute_match=counter_attribute_match,
            expected_unit=MESSAGE_COUNTER_UNIT,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        lost = counter_window_aggregate(
            samples,
            "robotics.message.lost",
            attribute_match=counter_attribute_match,
            expected_unit=MESSAGE_COUNTER_UNIT,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        sequence_errors = counter_window_aggregate(
            samples,
            "robotics.message.sequence_error",
            attribute_match=counter_attribute_match,
            expected_unit=MESSAGE_COUNTER_UNIT,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        if not (received.temporality == lost.temporality == sequence_errors.temporality):
            raise MetricAggregationError("message counters use different aggregation temporalities")
        if not (received.coverage == lost.coverage == sequence_errors.coverage):
            raise MetricAggregationError(
                "message counters do not cover the same collection intervals"
            )
        require_window_coverage(
            received.coverage,
            metric_name="message counters",
            temporality=received.temporality,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        if not (
            received.total.is_integer()
            and lost.total.is_integer()
            and sequence_errors.total.is_integer()
        ):
            raise MetricAggregationError("message counters must contain whole-message counts")
        sequence_integrity = sequence_errors.total == 0
        evaluations.append(
            AssertionEvaluation(
                assertion_id="data-plane-sequence-integrity",
                status="passed" if sequence_integrity else "failed",
                observed_value=sequence_errors.total,
                unit=MESSAGE_COUNTER_UNIT,
                message=(
                    ""
                    if sequence_integrity
                    else "DDS publication sequence metadata is unavailable or non-monotonic"
                ),
            )
        )
        total = received.total + lost.total
        if total <= 0:
            raise MetricAggregationError("message counters contain no observations")
        loss_ratio = lost.total / total
        passed = loss_ratio <= float(policy["max_loss_ratio"])
        evaluations.append(
            AssertionEvaluation(
                assertion_id="data-plane-loss-ratio",
                status="passed" if passed else "failed",
                observed_value=loss_ratio,
                unit="1",
                message=("" if passed else f"threshold lte {policy['max_loss_ratio']}"),
            )
        )
    except MetricAggregationError as error:
        if not any(
            evaluation.assertion_id == "data-plane-message-age" for evaluation in evaluations
        ):
            evaluations.append(
                AssertionEvaluation(
                    assertion_id="data-plane-message-age",
                    status="error",
                    observed_value=None,
                    unit="ms",
                    message=str(error),
                )
            )
        evaluations.append(
            AssertionEvaluation(
                assertion_id="data-plane-loss-ratio",
                status="error",
                observed_value=None,
                unit="1",
                message=str(error),
            )
        )
        if not any(
            evaluation.assertion_id == "data-plane-sequence-integrity" for evaluation in evaluations
        ):
            evaluations.append(
                AssertionEvaluation(
                    assertion_id="data-plane-sequence-integrity",
                    status="error",
                    observed_value=None,
                    unit=MESSAGE_COUNTER_UNIT,
                    message=str(error),
                )
            )
    return tuple(evaluations)


def evaluate_evidence_policy(
    policy: Mapping[str, Any],
    evidence: VerifiedEvidence,
) -> tuple[AssertionEvaluation, ...]:
    """Evaluate recording coverage and bounded-storage policy."""

    observation = evidence.index.data["policy_observation"]
    artifacts = evidence.index.data["artifacts"]
    summaries = evidence.recording_summaries
    channels = {
        channel["topic"]
        for summary in summaries
        for channel in summary.data["channels"]
        if channel["message_count"] > 0
    }
    required_topics = set(policy["topics"])
    durations_sec = [
        (
            summary.data["statistics"]["message_end_time_ns"]
            - summary.data["statistics"]["message_start_time_ns"]
        )
        / 1_000_000_000
        for summary in summaries
    ]
    compressions = {
        compression for summary in summaries for compression in summary.data["compressions"]
    }
    max_segment_size = max(
        (artifact["size_bytes"] for artifact in artifacts if artifact["kind"] == "recording"),
        default=0,
    )
    max_segment_duration = max(durations_sec, default=0.0)
    retention_classes = {artifact["retention_class"] for artifact in artifacts}
    expected_remote = policy["upload_mode"] != "local_only"
    spool_ratio = observation["spool_peak_size_bytes"] / policy["max_spool_size_bytes"]
    checks = (
        (
            "evidence-topics",
            required_topics <= channels,
            f"missing recorded topics: {sorted(required_topics - channels)}",
        ),
        (
            "evidence-recording-mode",
            observation["recording_mode"] == policy["recording_mode"],
            "recording mode differs",
        ),
        (
            "evidence-compression",
            compressions == {policy["compression"]}
            and observation["compression"] == policy["compression"],
            f"expected only {policy['compression']}; observed {sorted(compressions)}",
        ),
        (
            "evidence-segment-size",
            max_segment_size <= policy["max_segment_size_bytes"],
            "segment size limit exceeded",
        ),
        (
            "evidence-segment-duration",
            max_segment_duration <= policy["max_segment_duration_sec"],
            "segment duration limit exceeded",
        ),
        (
            "evidence-spool-size",
            observation["spool_peak_size_bytes"] <= policy["max_spool_size_bytes"],
            "spool size limit exceeded",
        ),
        (
            "evidence-spool-watermark",
            spool_ratio <= policy["spool_high_watermark_ratio"],
            "spool high-watermark ratio exceeded",
        ),
        (
            "evidence-upload-lag",
            observation["upload_lag_max_sec"] <= policy["max_upload_lag_sec"],
            "upload lag limit exceeded",
        ),
        (
            "evidence-upload-mode",
            observation["upload_mode"] == policy["upload_mode"],
            "upload mode differs",
        ),
        (
            "evidence-retention",
            retention_classes == {policy["retention_class"]},
            f"retention class differs: {sorted(retention_classes)}",
        ),
        (
            "evidence-remote-sink",
            observation["remote_sink_used"] == expected_remote
            and policy["remote_sink_allowed"] == expected_remote,
            "remote sink policy differs",
        ),
    )
    return tuple(
        _boolean_evaluation(f"policy-{name}", passed, message) for name, passed, message in checks
    )

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import pstdev
from typing import Any

from robotics_runtime_contracts import hardware_clock_within_policy

from robotics_acceptance_harness.metrics import MetricPoint, MetricSample
from robotics_acceptance_harness.timing import utc_datetime_from_unix_ns

OFFSET_METRIC = "robotics.hardware.clock.offset"
DRIFT_METRIC = "robotics.hardware.clock.drift"
AGE_METRIC = "robotics.hardware.message.age"
MONOTONIC_METRIC = "robotics.hardware.clock.monotonic"
PROTOCOL_ATTRIBUTE = "robotics.clock.sync_protocol"
SOURCE_ATTRIBUTE = "robotics.clock.source"

_METRIC_UNITS = {
    OFFSET_METRIC: "ms",
    DRIFT_METRIC: "ppm",
    AGE_METRIC: "ms",
    MONOTONIC_METRIC: "1",
}
_SUPPORTED_SOURCES = {
    "pmc",
    "chronyc_tracking",
    "mavlink_timesync_status",
    "controller_telemetry",
    "external_attestation",
}


class HardwareTimingInputError(ValueError):
    """Raised when hardware timing evidence is incomplete or contradictory."""


@dataclass(frozen=True, slots=True)
class HardwareTimingObservation:
    sync_protocol: str
    source: str
    measured_at: datetime
    sample_count: int
    offset_ms: float
    jitter_ms: float
    drift_ppm: float
    max_sample_age_ms: float
    monotonic: bool
    within_policy: bool


def _timing_samples(samples: Sequence[MetricPoint]) -> dict[str, list[MetricSample]]:
    grouped: dict[str, list[MetricSample]] = defaultdict(list)
    for sample in samples:
        if sample.name in _METRIC_UNITS:
            if not isinstance(sample, MetricSample):
                raise HardwareTimingInputError(f"{sample.name} must be an OTLP gauge or sum point")
            grouped[sample.name].append(sample)
    missing = sorted(set(_METRIC_UNITS) - set(grouped))
    if missing:
        raise HardwareTimingInputError(f"missing hardware timing metrics: {', '.join(missing)}")
    return grouped


def _metadata(grouped: Mapping[str, Sequence[MetricSample]]) -> tuple[str, str]:
    protocols: set[str] = set()
    sources: set[str] = set()
    for name, samples in grouped.items():
        expected_unit = _METRIC_UNITS[name]
        for sample in samples:
            if sample.unit != expected_unit:
                raise HardwareTimingInputError(
                    f"{name} requires unit {expected_unit}; received {sample.unit}"
                )
            protocol = sample.attributes.get(PROTOCOL_ATTRIBUTE)
            source = sample.attributes.get(SOURCE_ATTRIBUTE)
            if not isinstance(protocol, str) or not isinstance(source, str):
                raise HardwareTimingInputError(
                    f"{name} requires string attributes {PROTOCOL_ATTRIBUTE} and {SOURCE_ATTRIBUTE}"
                )
            protocols.add(protocol)
            sources.add(source)
    if len(protocols) != 1 or len(sources) != 1:
        raise HardwareTimingInputError("hardware timing metadata changed during observation")
    protocol = protocols.pop()
    source = sources.pop()
    if source not in _SUPPORTED_SOURCES:
        raise HardwareTimingInputError(f"unsupported hardware timing source: {source}")
    return protocol, source


def _aligned_points(
    grouped: Mapping[str, Sequence[MetricSample]],
) -> tuple[tuple[int, Mapping[str, float]], ...]:
    points: dict[int, dict[str, float]] = defaultdict(dict)
    for name, samples in grouped.items():
        for sample in samples:
            if name in points[sample.observed_at_ns]:
                raise HardwareTimingInputError(f"duplicate {name} point at {sample.observed_at_ns}")
            points[sample.observed_at_ns][name] = sample.value
    expected = set(_METRIC_UNITS)
    for timestamp, values in points.items():
        if set(values) != expected:
            missing = sorted(expected - set(values))
            raise HardwareTimingInputError(
                f"unaligned hardware timing point at {timestamp}; missing {', '.join(missing)}"
            )
    return tuple((timestamp, points[timestamp]) for timestamp in sorted(points))


def evaluate_hardware_timing(
    time_policy: Mapping[str, Any],
    samples: Sequence[MetricPoint],
) -> HardwareTimingObservation:
    """Evaluate host-produced hardware clock measurements from standard OTLP points."""

    grouped = _timing_samples(samples)
    protocol, source = _metadata(grouped)
    if protocol != time_policy["clock_sync_protocol"]:
        raise HardwareTimingInputError(
            "hardware timing protocol does not match scenario time policy"
        )
    points = _aligned_points(grouped)
    offsets = [values[OFFSET_METRIC] for _, values in points]
    drifts = [values[DRIFT_METRIC] for _, values in points]
    ages = [values[AGE_METRIC] for _, values in points]
    monotonic = all(values[MONOTONIC_METRIC] == 1.0 for _, values in points)
    offset_ms = max(abs(value) for value in offsets)
    drift_ppm = max(abs(value) for value in drifts)
    max_sample_age_ms = max(ages)
    jitter_ms = pstdev(offsets)
    within_policy = hardware_clock_within_policy(
        time_policy,
        {
            "sample_count": len(points),
            "offset_ms": offset_ms,
            "drift_ppm": drift_ppm,
            "max_sample_age_ms": max_sample_age_ms,
        },
        monotonic=monotonic,
    )
    measured_at = utc_datetime_from_unix_ns(points[-1][0])
    return HardwareTimingObservation(
        sync_protocol=protocol,
        source=source,
        measured_at=measured_at,
        sample_count=len(points),
        offset_ms=offset_ms,
        jitter_ms=jitter_ms,
        drift_ppm=drift_ppm,
        max_sample_age_ms=max_sample_age_ms,
        monotonic=monotonic,
        within_policy=within_policy,
    )

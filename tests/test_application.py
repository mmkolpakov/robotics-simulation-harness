from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from junitparser import JUnitXml

from robotics_acceptance_harness.application import (
    VerificationError,
    evaluate_from_evidence,
    run_verification,
)
from robotics_acceptance_harness.documents import DocumentBundle, load_bundle
from robotics_acceptance_harness.readiness import GraphSnapshot, TopicObservation
from robotics_acceptance_harness.time_authority import DELIVERY_LATENCY_METRIC
from robotics_acceptance_harness.timing import ClockSample
from tests.support import (
    FakeTime,
    acceptance_run,
    local_evidence_artifact,
    local_recording_artifact,
    write_evidence_index,
)

FIXTURES = Path(__file__).parent / "fixtures" / "simulation"
PHYSICAL_FIXTURES = Path(__file__).parent / "fixtures" / "physical"
SIMULATION_RUN_ID = "run-6ba7b810-9dad-41d1-80b4-00c04fd430c8"
PHYSICAL_RUN_ID = "run-6ba7b811-9dad-41d1-80b4-00c04fd430c8"
SIMULATION_DOMAIN = "camera-domain"
PHYSICAL_DOMAIN = "controller-domain"


class FakeObserver:
    def __init__(
        self,
        clock: FakeTime,
        *,
        source_scale: float = 1.0,
        forbidden_publishers: int = 0,
        physical: bool = False,
    ) -> None:
        self.clock = clock
        self.source_scale = source_scale
        self.forbidden_publishers = forbidden_publishers
        self.physical = physical
        self._clock_samples: list[ClockSample] = []
        self._record_clock = True
        self.closed = False

    @property
    def clock_samples(self) -> tuple[ClockSample, ...]:
        return tuple(self._clock_samples)

    def start_clock_observation(self) -> None:
        self._clock_samples.clear()
        self._record_clock = True

    def stop_clock_observation(self) -> tuple[ClockSample, ...]:
        self._record_clock = False
        return self.clock_samples

    def snapshot(self) -> GraphSnapshot:
        if self.physical:
            return GraphSnapshot(
                observed_at_ns=self.clock.value_ns,
                topics={
                    "/cmd_vel": TopicObservation(
                        types=("geometry_msgs/msg/Twist",),
                        publishers=self.forbidden_publishers,
                        subscribers=0,
                    )
                },
            )
        if self._record_clock and (
            not self._clock_samples or self._clock_samples[-1].observed_at_ns != self.clock.value_ns
        ):
            self._clock_samples.append(
                ClockSample(
                    self.clock.value_ns,
                    int(self.clock.value_ns * self.source_scale),
                )
            )
        return GraphSnapshot(
            observed_at_ns=self.clock.value_ns,
            topics={
                "/clock": TopicObservation(
                    types=("rosgraph_msgs/msg/Clock",),
                    publishers=1,
                    subscribers=0,
                    first_message_at_ns=0,
                )
            },
        )

    def close(self) -> None:
        self.closed = True


class LegacyFakeObserver:
    def __init__(self, clock: FakeTime, *, source_scale: float = 1.0) -> None:
        self._delegate = FakeObserver(clock, source_scale=source_scale)

    @property
    def clock_samples(self) -> tuple[ClockSample, ...]:
        return self._delegate.clock_samples

    def snapshot(self) -> GraphSnapshot:
        return self._delegate.snapshot()

    def close(self) -> None:
        self._delegate.close()


def _attributes(values: Mapping[str, str]) -> list[dict[str, object]]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def _histogram(
    name: str,
    unit: str,
    *,
    start_ns: int,
    end_ns: int,
    count: int,
    value: float,
) -> dict[str, object]:
    bounds = (0.5, 2.0, 5.0)
    bucket = next((index for index, bound in enumerate(bounds) if value <= bound), len(bounds))
    counts = [0] * (len(bounds) + 1)
    counts[bucket] = count
    return {
        "name": name,
        "unit": unit,
        "histogram": {
            "aggregationTemporality": 1,
            "dataPoints": [
                {
                    "startTimeUnixNano": str(start_ns),
                    "timeUnixNano": str(end_ns),
                    "count": str(count),
                    "sum": value * count,
                    "min": value,
                    "max": value,
                    "bucketCounts": [str(item) for item in counts],
                    "explicitBounds": list(bounds),
                }
            ],
        },
    }


def _sum(name: str, value: int, *, start_ns: int, end_ns: int) -> dict[str, object]:
    return {
        "name": name,
        "unit": "{message}",
        "sum": {
            "aggregationTemporality": 1,
            "isMonotonic": True,
            "dataPoints": [
                {
                    "startTimeUnixNano": str(start_ns),
                    "timeUnixNano": str(end_ns),
                    "asInt": str(value),
                    "attributes": _attributes(
                        {
                            "sequence.measurement.method": (
                                "rmw_publication_sequence_single_publisher"
                            )
                        }
                    ),
                }
            ],
        },
    }


def _gauge(
    name: str,
    unit: str,
    points: tuple[tuple[int, float], ...],
    *,
    attributes: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "unit": unit,
        "gauge": {
            "dataPoints": [
                {
                    "timeUnixNano": str(timestamp),
                    "asDouble": value,
                    **({"attributes": _attributes(attributes)} if attributes else {}),
                }
                for timestamp, value in points
            ]
        },
    }


def _write_metrics(
    path: Path,
    *,
    run_id: str,
    domain_id: str,
    source_id: str,
    start_ns: int,
    end_ns: int,
    hardware_offset_ms: float | None = None,
) -> None:
    metrics = [
        _histogram(
            DELIVERY_LATENCY_METRIC,
            "ms",
            start_ns=start_ns,
            end_ns=end_ns,
            count=30,
            value=0.1,
        ),
        _histogram(
            "robotics.message.age",
            "ms",
            start_ns=start_ns,
            end_ns=end_ns,
            count=1,
            value=1,
        ),
        _sum("robotics.message.received", 100, start_ns=start_ns, end_ns=end_ns),
        _sum("robotics.message.lost", 0, start_ns=start_ns, end_ns=end_ns),
        _sum("robotics.message.sequence_error", 0, start_ns=start_ns, end_ns=end_ns),
        _gauge(
            "robotics.simulation.deadline_miss_ratio",
            "1",
            ((end_ns, 0),),
        ),
    ]
    if hardware_offset_ms is not None:
        hardware_attributes = {
            "robotics.clock.sync_protocol": "mavlink_timesync",
            "robotics.clock.source": "mavlink_timesync_status",
        }
        hardware_timestamps = tuple(
            start_ns + ((end_ns - start_ns) * index // 29) for index in range(30)
        )
        for name, unit, value in (
            (
                "robotics.hardware.clock.offset",
                "ms",
                hardware_offset_ms,
            ),
            ("robotics.hardware.clock.drift", "ppm", 2.0),
            ("robotics.hardware.message.age", "ms", 5.0),
            ("robotics.hardware.clock.monotonic", "1", 1.0),
        ):
            metrics.append(
                _gauge(
                    name,
                    unit,
                    tuple(
                        (
                            timestamp,
                            value
                            + (
                                0.1
                                if name == "robotics.hardware.clock.offset" and index == 29
                                else 0.0
                            ),
                        )
                        for index, timestamp in enumerate(hardware_timestamps)
                    ),
                    attributes=hardware_attributes,
                )
            )
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": _attributes(
                        {
                            "run.id": run_id,
                            "domain.id": domain_id,
                            "time.source.id": source_id,
                            "time.measurement.method": "rmw_source_to_reception_latency",
                            "channel": "/robotics/runtime_probe",
                        }
                    )
                },
                "scopeMetrics": [{"metrics": metrics}],
            }
        ]
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _simulation_bundle(tmp_path: Path) -> DocumentBundle:
    scenario = yaml.safe_load((FIXTURES / "scenario.yaml").read_text(encoding="utf-8"))
    scenario["timeouts"]["stable_for_sec"] = 0
    scenario["timeouts"]["execution_sec"] = 0.2
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return load_bundle(scenario_path, runtime_path=FIXTURES / "runtime.yaml")


def _physical_bundle() -> DocumentBundle:
    return load_bundle(
        PHYSICAL_FIXTURES / "hil-scenario.yaml",
        runtime_path=PHYSICAL_FIXTURES / "hil-runtime.json",
        permit_path=PHYSICAL_FIXTURES / "hil-permit.json",
        verification_path=PHYSICAL_FIXTURES / "hil-verification.json",
        now=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )


def _write_run_context(
    path: Path,
    bundle: DocumentBundle,
    *,
    run_id: str,
    domain_id: str,
    time_kind: str,
    source_id: str,
) -> Path:
    path.write_text(
        yaml.safe_dump(
            acceptance_run(
                run_id=run_id,
                scenario_id=str(bundle.scenario.data["scenario_id"]),
                scenario_sha256=bundle.scenario.sha256,
                time_kind=time_kind,
                source_id=source_id,
                domains=[{"domain_id": domain_id, "role": "observer"}],
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_evidence(
    path: Path,
    metrics_path: Path,
    *,
    run_id: str,
    physical: bool = False,
    include_metrics: bool = True,
) -> Path:
    retention = "hil-30d" if physical else "pull-request-7d"
    topics = {} if physical else {"/clock": "rosgraph_msgs/msg/Clock"}
    artifacts = [
        local_recording_artifact(
            path.with_suffix(".mcap"),
            topics=topics,
            retention_class=retention,
        )
    ]
    if include_metrics:
        artifacts.append(
            local_evidence_artifact(
                metrics_path,
                media_type="application/x-ndjson",
                artifact_index=1,
                retention_class=retention,
            )
        )
    return write_evidence_index(
        path,
        run_id=run_id,
        recording_mode="bounded" if physical else "on_failure",
        upload_mode="closed_segments_during_run" if physical else "local_only",
        artifacts=artifacts,
    )


def _run_case(
    tmp_path: Path,
    *,
    bundle: DocumentBundle,
    run_id: str,
    domain_id: str,
    run_context_path: Path,
    evidence_path: Path,
    metrics_path: Path,
    measurement_complete_path: Path,
    observer: FakeObserver | LegacyFakeObserver,
    clock: FakeTime,
    window: tuple[int, int],
    interval: tuple[datetime, datetime],
    sleep_fn: Callable[[float], None] | None = None,
):
    wall_times = iter(window)
    utc_times = iter(interval)
    return run_verification(
        run_id=run_id,
        domain_id=domain_id,
        run_context_path=run_context_path,
        bundle=bundle,
        evidence_index_path=evidence_path,
        otel_metrics_path=metrics_path,
        measurement_complete_path=measurement_complete_path,
        output_dir=tmp_path / "output",
        observer_factory=lambda *_args, **_kwargs: observer,
        now_ns=clock.now_ns,
        wall_time_ns=lambda: next(wall_times),
        sleep_fn=sleep_fn or clock.sleep,
        utc_now=lambda: next(utc_times),
        poll_interval_sec=0.05,
    )


def _simulation_case(
    tmp_path: Path,
    *,
    metrics_run_id: str = SIMULATION_RUN_ID,
    metric_window: tuple[int, int] = (1_000_000_000, 2_000_000_000),
    source_scale: float = 1,
    include_metrics: bool = True,
    observer_type: type[FakeObserver] | type[LegacyFakeObserver] = FakeObserver,
):
    bundle = _simulation_bundle(tmp_path)
    metrics_path = tmp_path / "metrics.otlp.json"
    _write_metrics(
        metrics_path,
        run_id=metrics_run_id,
        domain_id=SIMULATION_DOMAIN,
        source_id="simulation-clock",
        start_ns=metric_window[0],
        end_ns=metric_window[1],
    )
    evidence_path = _write_evidence(
        tmp_path / "evidence.yaml",
        metrics_path,
        run_id=SIMULATION_RUN_ID,
        include_metrics=include_metrics,
    )
    context_path = _write_run_context(
        tmp_path / "run.yaml",
        bundle,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        time_kind="sim_clock",
        source_id="simulation-clock",
    )
    clock = FakeTime()
    observer = observer_type(clock, source_scale=source_scale)
    started = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    outputs = _run_case(
        tmp_path,
        bundle=bundle,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        run_context_path=context_path,
        evidence_path=evidence_path,
        metrics_path=metrics_path,
        measurement_complete_path=tmp_path / "measurement-complete",
        observer=observer,
        clock=clock,
        window=(1_000_000_000, 2_000_000_000),
        interval=(started, started + timedelta(seconds=1)),
    )
    return outputs


def _physical_case(
    tmp_path: Path,
    *,
    forbidden_publishers: int = 0,
    offset_ms: float = 0.5,
):
    bundle = _physical_bundle()
    started = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    start_ns = int(started.timestamp() * 1_000_000_000)
    end_ns = start_ns + 123 * 1_000_000_000
    metrics_path = tmp_path / "hardware.otlp.json"
    _write_metrics(
        metrics_path,
        run_id=PHYSICAL_RUN_ID,
        domain_id=PHYSICAL_DOMAIN,
        source_id="controller-clock",
        start_ns=start_ns,
        end_ns=end_ns,
        hardware_offset_ms=offset_ms,
    )
    clock = FakeTime()
    observer = FakeObserver(
        clock,
        forbidden_publishers=forbidden_publishers,
        physical=True,
    )
    outputs = _run_case(
        tmp_path,
        bundle=bundle,
        run_id=PHYSICAL_RUN_ID,
        domain_id=PHYSICAL_DOMAIN,
        run_context_path=_write_run_context(
            tmp_path / "run.yaml",
            bundle,
            run_id=PHYSICAL_RUN_ID,
            domain_id=PHYSICAL_DOMAIN,
            time_kind="mavlink_timesync",
            source_id="controller-clock",
        ),
        evidence_path=_write_evidence(
            tmp_path / "evidence.yaml",
            metrics_path,
            run_id=PHYSICAL_RUN_ID,
            physical=True,
        ),
        metrics_path=metrics_path,
        measurement_complete_path=tmp_path / "measurement-complete",
        observer=observer,
        clock=clock,
        window=(start_ns, end_ns),
        interval=(started, started + timedelta(seconds=123)),
    )
    return outputs, observer


def test_verification_accepts_legacy_observer_factory(tmp_path: Path) -> None:
    outputs = _simulation_case(tmp_path, observer_type=LegacyFakeObserver)

    assert outputs.result["status"] == "passed"


def test_verification_finalizes_measurement_before_reading_evidence(tmp_path: Path) -> None:
    bundle = _simulation_bundle(tmp_path)
    metrics_path = tmp_path / "metrics.otlp.json"
    _write_metrics(
        metrics_path,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        source_id="simulation-clock",
        start_ns=1_000_000_000,
        end_ns=2_000_000_000,
    )
    pending = _write_evidence(
        tmp_path / "pending.yaml",
        metrics_path,
        run_id=SIMULATION_RUN_ID,
    )
    evidence_path = tmp_path / "evidence.yaml"
    marker = tmp_path / "measurement-complete"
    context = _write_run_context(
        tmp_path / "run.yaml",
        bundle,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        time_kind="sim_clock",
        source_id="simulation-clock",
    )
    clock = FakeTime()
    observer = FakeObserver(clock)

    def finalize_after_marker(seconds: float) -> None:
        clock.sleep(seconds)
        if marker.is_file() and pending.exists():
            pending.replace(evidence_path)

    started = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    outputs = _run_case(
        tmp_path,
        bundle=bundle,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        run_context_path=context,
        evidence_path=evidence_path,
        metrics_path=metrics_path,
        measurement_complete_path=marker,
        observer=observer,
        clock=clock,
        window=(1_000_000_000, 2_000_000_000),
        interval=(started, started + timedelta(seconds=1)),
        sleep_fn=finalize_after_marker,
    )

    assert outputs.result["status"] == "passed"
    assert outputs.result["shutdown"] == {
        "observer_detached": True,
        "recorders_closed": True,
        "evidence_index_finalized": True,
    }
    assert marker.is_file()
    assert observer.closed


def test_offline_evaluation_reuses_retained_gazebo_evidence(tmp_path: Path) -> None:
    bundle = _simulation_bundle(tmp_path)
    metrics_path = tmp_path / "metrics.otlp.json"
    _write_metrics(
        metrics_path,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        source_id="simulation-clock",
        start_ns=1_000_000_000,
        end_ns=2_000_000_000,
    )
    evidence_path = _write_evidence(
        tmp_path / "evidence.yaml",
        metrics_path,
        run_id=SIMULATION_RUN_ID,
    )
    context_path = _write_run_context(
        tmp_path / "run.yaml",
        bundle,
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        time_kind="sim_clock",
        source_id="simulation-clock",
    )

    outputs = evaluate_from_evidence(
        run_id=SIMULATION_RUN_ID,
        domain_id=SIMULATION_DOMAIN,
        run_context_path=context_path,
        bundle=bundle,
        evidence_index_path=evidence_path,
        otel_metrics_path=metrics_path,
        window_start_ns=1_000_000_000,
        window_end_ns=2_000_000_000,
        output_dir=tmp_path / "offline-output",
    )

    assert outputs.result["evaluation_mode"] == "offline"
    assert outputs.result["status"] == "incomplete"
    assert "$.observed_ros_graph" in outputs.result["unevaluated"]


def test_verification_rejects_a_stale_measurement_marker_before_observation(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "measurement-complete"
    marker.touch()

    with pytest.raises(VerificationError, match="marker already exists"):
        _simulation_case(tmp_path)


def test_verification_requires_metrics_to_be_verified_evidence(tmp_path: Path) -> None:
    with pytest.raises(VerificationError, match="verified local application/x-ndjson"):
        _simulation_case(tmp_path, include_metrics=False)


def test_time_authority_rejects_samples_outside_measurement_window(tmp_path: Path) -> None:
    outputs = _simulation_case(tmp_path, metric_window=(100, 130))

    authority = outputs.result["time_authority_observation"]
    assert authority["sample_count"] == 0
    assert authority["within_policy"] is False
    assert outputs.result["status"] != "passed"


def test_verification_ignores_metrics_from_another_run(tmp_path: Path) -> None:
    outputs = _simulation_case(
        tmp_path,
        metrics_run_id="run-00000000-0000-4000-8000-000000000001",
    )

    assert outputs.result["time_authority_observation"]["sample_count"] == 0
    assert outputs.result["status"] == "error"


def test_valid_latency_does_not_hide_a_frozen_simulation_clock(tmp_path: Path) -> None:
    outputs = _simulation_case(tmp_path, source_scale=0)

    timing = next(
        item
        for item in outputs.result["assertion_results"]
        if item["assertion_id"] == "time-policy"
    )
    assert outputs.result["time_authority_observation"]["within_policy"] is True
    assert timing["status"] == "failed"
    assert outputs.result["clock_observation"]["real_time_factor"] == 0
    assert outputs.result["status"] == "failed"


def test_physical_verification_emits_authorized_result(tmp_path: Path) -> None:
    outputs, observer = _physical_case(tmp_path)
    result = outputs.result

    assert result["schema_version"] == "acceptance-result.v1"
    assert result["evaluation_mode"] == "live"
    assert result["status"] == "passed"
    assert result["authorization"]["mode"] == "verified_execution_permit"
    assert result["hardware_clock_observation"]["within_policy"] is True
    assert JUnitXml.fromfile(outputs.junit_path).failures == 0
    assert observer.closed


def test_physical_verification_detects_transient_command_publisher(tmp_path: Path) -> None:
    outputs, _observer = _physical_case(tmp_path, forbidden_publishers=1)

    assert outputs.result["status"] == "failed"
    assert outputs.result["forbidden_graph_observation"]["violations"] == [
        {"kind": "topic", "name": "/cmd_vel"}
    ]


def test_physical_verification_enforces_hardware_clock_policy(tmp_path: Path) -> None:
    outputs, _observer = _physical_case(tmp_path, offset_ms=10)

    assert outputs.result["status"] == "failed"
    assert outputs.result["hardware_clock_observation"]["within_policy"] is False

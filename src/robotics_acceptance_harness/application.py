from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns, sleep, time_ns
from typing import Any, Protocol, cast
from uuid import uuid4

from robotics_acceptance_harness.documents import DocumentBundle
from robotics_acceptance_harness.evaluation import EvaluationContext, evaluate_acceptance
from robotics_acceptance_harness.evidence import VerifiedEvidence, load_evidence_index
from robotics_acceptance_harness.forbidden_graph import (
    ForbiddenGraphMonitor,
    ForbiddenGraphObservation,
)
from robotics_acceptance_harness.hardware_timing import (
    HardwareTimingObservation,
    evaluate_hardware_timing,
)
from robotics_acceptance_harness.metrics import (
    AssertionEvaluation,
    HistogramSample,
    MetricPoint,
    MetricSample,
)
from robotics_acceptance_harness.otel import (
    OTLP_JSON_LINES_MEDIA_TYPE,
    load_otlp_json_metrics,
    select_metric_points,
)
from robotics_acceptance_harness.readiness import (
    GraphObserver,
    GraphSnapshot,
    ReadinessResult,
    wait_for_readiness,
)
from robotics_acceptance_harness.receipts import VerifiedReceiptSet
from robotics_acceptance_harness.result import (
    build_acceptance_result,
    write_contract_json,
    write_junit_xml,
)
from robotics_acceptance_harness.ros import RosGraphObserver
from robotics_acceptance_harness.run_context import load_run_context
from robotics_acceptance_harness.time_authority import evaluate_time_authority
from robotics_acceptance_harness.timing import (
    ClockSample,
    TimingObservation,
    TimingValidationError,
    evaluate_timing,
    utc_datetime_from_unix_ns,
)


class ClockObserver(GraphObserver, Protocol):
    @property
    def clock_samples(self) -> tuple[ClockSample, ...]: ...


class WindowedClockObserver(Protocol):
    def start_clock_observation(self) -> None: ...

    def stop_clock_observation(self) -> tuple[ClockSample, ...]: ...


class VerificationError(RuntimeError):
    """Raised when an execution cannot produce an acceptance result."""


@dataclass(frozen=True, slots=True)
class VerificationOutputs:
    result: Mapping[str, Any]
    result_path: Path
    junit_path: Path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def explain_bundle(bundle: DocumentBundle) -> dict[str, Any]:
    """Return the validated execution facts without starting an observation."""

    scenario = bundle.scenario_data
    workload_kind = bundle.runtime_data["workload"]["kind"]
    return {
        "schema_version": bundle.scenario.schema_version,
        "scenario_id": scenario["scenario_id"],
        "scenario_sha256": bundle.scenario.sha256,
        "runtime_manifest_sha256": bundle.runtime.sha256,
        "execution": dict(scenario["execution"]),
        "workload_kind": workload_kind,
        "model_manifest_sha256": bundle.model.sha256 if bundle.model else None,
        "dataset_manifest_sha256": bundle.dataset.sha256 if bundle.dataset else None,
        "permit_sha256": bundle.permit.sha256 if bundle.permit else None,
        "execution_verification_sha256": (
            bundle.verification.sha256 if bundle.verification else None
        ),
        "expected_ros_graph": {
            kind: len(scenario["expected_ros_graph"][kind])
            for kind in ("topics", "services", "actions", "lifecycle_nodes")
        },
        "evidence": {
            "recording_mode": scenario["evidence_policy"]["recording_mode"],
            "upload_mode": scenario["evidence_policy"]["upload_mode"],
            "retention_class": scenario["evidence_policy"]["retention_class"],
        },
        "unevaluated": [],
        "policy": (
            "accepted-simulation"
            if scenario["execution"]["target_environment"] == "simulation"
            else "authorized-physical-observation"
        ),
    }


def _latest_metric(samples: Sequence[MetricPoint], name: str) -> float | None:
    matches = [
        sample for sample in samples if isinstance(sample, MetricSample) and sample.name == name
    ]
    if not matches:
        return None
    return max(matches, key=lambda sample: sample.observed_at_ns).value


def _measurement_metrics(
    samples: Sequence[MetricPoint],
    *,
    window_start_ns: int,
    window_end_ns: int,
) -> tuple[MetricPoint, ...]:
    return tuple(
        sample
        for sample in samples
        if sample.observed_at_ns <= window_end_ns
        and (
            sample.observed_at_ns >= window_start_ns
            or (isinstance(sample, HistogramSample) and sample.temporality == "cumulative")
            or (
                isinstance(sample, MetricSample)
                and sample.instrument_kind == "sum"
                and sample.temporality == "cumulative"
            )
        )
    )


def _enrich_clock_samples(
    mode: str,
    samples: Sequence[ClockSample],
    metrics: Sequence[MetricPoint],
) -> tuple[ClockSample, ...]:
    if mode != "simulation_realtime" or len(samples) < 2:
        return tuple(samples)

    ratios: list[float] = []
    for previous, current in zip(samples, samples[1:], strict=False):
        wall_delta = current.observed_at_ns - previous.observed_at_ns
        source_delta = current.source_time_ns - previous.source_time_ns
        ratios.append(source_delta / wall_delta if wall_delta > 0 else 0.0)
    deadline_ratio = _latest_metric(metrics, "robotics.simulation.deadline_miss_ratio")
    return tuple(
        ClockSample(
            observed_at_ns=sample.observed_at_ns,
            source_time_ns=sample.source_time_ns,
            real_time_factor=ratios[min(index, len(ratios) - 1)],
            deadline_miss_ratio=deadline_ratio,
        )
        for index, sample in enumerate(samples)
    )


def _wait_for_evidence(
    path: str | Path,
    *,
    run_id: str,
    receipt_paths: Sequence[str | Path],
    verification_paths: Sequence[str | Path],
    receipt_dependency_paths: Sequence[str | Path],
    timeout_sec: float,
    poll_interval_sec: float,
    now_ns: Callable[[], int],
    sleep_fn: Callable[[float], None],
) -> VerifiedEvidence:
    source = Path(path).expanduser().resolve()
    deadline_ns = now_ns() + int(timeout_sec * 1_000_000_000)
    while not source.is_file():
        if now_ns() >= deadline_ns:
            raise VerificationError(f"finalized evidence index did not appear: {source}")
        remaining_sec = max(0.0, (deadline_ns - now_ns()) / 1_000_000_000)
        sleep_fn(min(poll_interval_sec, remaining_sec))
    return load_evidence_index(
        source,
        expected_run_id=run_id,
        receipt_paths=receipt_paths,
        verification_paths=verification_paths,
        receipt_dependency_paths=receipt_dependency_paths,
    )


def run_verification(
    *,
    run_id: str,
    bundle: DocumentBundle,
    domain_id: str,
    run_context_path: str | Path,
    evidence_index_path: str | Path,
    artifact_receipt_paths: Sequence[str | Path] = (),
    artifact_verification_paths: Sequence[str | Path] = (),
    receipt_dependency_paths: Sequence[str | Path] = (),
    evaluator_receipts: VerifiedReceiptSet | None = None,
    otel_metrics_path: str | Path,
    measurement_complete_path: str | Path,
    output_dir: str | Path,
    observer_factory: Callable[..., ClockObserver] = RosGraphObserver,
    now_ns: Callable[[], int] = monotonic_ns,
    wall_time_ns: Callable[[], int] = time_ns,
    sleep_fn: Callable[[float], None] = sleep,
    utc_now: Callable[[], datetime] = _utc_now,
    poll_interval_sec: float = 0.05,
) -> VerificationOutputs:
    """Attach to a running execution and produce canonical acceptance outputs."""

    scenario = bundle.scenario_data
    execution = scenario["execution"]
    physical = execution["target_environment"] in {"hil", "real_robot"}
    measurement_complete = Path(measurement_complete_path).expanduser().resolve()
    if measurement_complete.exists():
        raise VerificationError(
            f"measurement completion marker already exists: {measurement_complete}"
        )
    if not measurement_complete.parent.is_dir():
        raise VerificationError(
            f"measurement completion directory does not exist: {measurement_complete.parent}"
        )
    run_context = load_run_context(
        run_context_path,
        run_id=run_id,
        domain_id=domain_id,
        scenario_id=str(scenario["scenario_id"]),
        scenario_sha256=bundle.scenario.sha256,
    )
    result_id = f"result-{uuid4()}"
    observe_clock = execution["time_mode"] != "hardware_realtime"
    forbidden_monitor = ForbiddenGraphMonitor(scenario["forbidden_ros_graph"])
    observer = observer_factory(
        scenario["expected_ros_graph"],
        observe_clock=observe_clock,
        forbidden_graph=scenario["forbidden_ros_graph"],
    )
    started_at = utc_now()
    measurement_started_ns = 0
    measurement_finished_ns = 0
    measurement_started_monotonic_ns = 0
    measurement_finished_monotonic_ns = 0
    last_snapshot = None
    try:
        readiness = wait_for_readiness(
            scenario["expected_ros_graph"],
            observer,
            timeout_sec=float(scenario["timeouts"]["graph_ready_sec"]),
            stable_for_sec=float(scenario["timeouts"]["stable_for_sec"]),
            poll_interval_sec=poll_interval_sec,
            now_ns=now_ns,
            sleep_fn=sleep_fn,
            on_snapshot=forbidden_monitor.observe,
        )
        start_clock_observation = getattr(observer, "start_clock_observation", None)
        stop_clock_observation = getattr(observer, "stop_clock_observation", None)
        windowed_observer = (
            cast(WindowedClockObserver, observer)
            if callable(start_clock_observation) and callable(stop_clock_observation)
            else None
        )
        if windowed_observer is not None:
            windowed_observer.start_clock_observation()
        measurement_started_ns = wall_time_ns()
        measurement_started_monotonic_ns = now_ns()
        deadline_ns = measurement_started_monotonic_ns + int(
            float(scenario["timeouts"]["execution_sec"]) * 1_000_000_000
        )
        last_snapshot = readiness.snapshot
        while now_ns() < deadline_ns:
            last_snapshot = observer.snapshot()
            forbidden_monitor.observe(last_snapshot)
            remaining_sec = max(0.0, (deadline_ns - now_ns()) / 1_000_000_000)
            sleep_fn(min(poll_interval_sec, remaining_sec))
        measurement_finished_monotonic_ns = now_ns()
        measurement_finished_ns = wall_time_ns()
        observed_clock_samples = (
            windowed_observer.stop_clock_observation()
            if windowed_observer is not None
            else observer.clock_samples
        )
        if measurement_finished_ns < measurement_started_ns:
            raise VerificationError("measurement wall clock moved backwards")
        raw_clock_samples = tuple(
            sample
            for sample in observed_clock_samples
            if measurement_started_monotonic_ns
            <= sample.observed_at_ns
            <= measurement_finished_monotonic_ns
        )
    finally:
        observer.close()

    try:
        measurement_complete.touch(mode=0o444, exist_ok=False)
    except FileExistsError as error:
        raise VerificationError(
            f"measurement completion marker appeared during the run: {measurement_complete}"
        ) from error

    assert last_snapshot is not None
    evidence = _wait_for_evidence(
        evidence_index_path,
        run_id=run_id,
        receipt_paths=artifact_receipt_paths,
        verification_paths=artifact_verification_paths,
        receipt_dependency_paths=receipt_dependency_paths,
        timeout_sec=float(scenario["timeouts"]["shutdown_sec"]),
        poll_interval_sec=poll_interval_sec,
        now_ns=now_ns,
        sleep_fn=sleep_fn,
    )
    metrics_path = Path(otel_metrics_path).expanduser().resolve()
    metric_link = evidence.local_files.get(metrics_path)
    if metric_link is None or metric_link["media_type"] != OTLP_JSON_LINES_MEDIA_TYPE:
        raise VerificationError(
            f"OTLP metrics must be verified local {OTLP_JSON_LINES_MEDIA_TYPE} evidence"
        )
    metrics_evidence_sha256 = str(metric_link["sha256"])
    metric_samples = select_metric_points(
        load_otlp_json_metrics(
            metrics_path,
            expected_sha256=metrics_evidence_sha256,
        ),
        run_id=run_id,
        domain_id=domain_id,
    )
    metric_samples = _measurement_metrics(
        metric_samples,
        window_start_ns=measurement_started_ns,
        window_end_ns=measurement_finished_ns,
    )
    readiness = ReadinessResult(
        snapshot=last_snapshot,
        first_ready_at_ns=readiness.first_ready_at_ns,
        stable_for_sec=readiness.stable_for_sec,
    )
    hardware_timing: HardwareTimingObservation | None = None
    timing_failure: AssertionEvaluation | None = None
    if physical:
        hardware_timing = evaluate_hardware_timing(
            scenario["time_policy"],
            tuple(sample for sample in metric_samples if isinstance(sample, MetricSample)),
        )
        timing = TimingObservation(
            monotonic=hardware_timing.monotonic,
            offset_ms=hardware_timing.offset_ms,
            drift_ppm=hardware_timing.drift_ppm,
            real_time_factor=0.0,
            deadline_miss_ratio=0.0,
            max_message_age_ms=hardware_timing.max_sample_age_ms,
            clock_hz=0.0,
        )
    else:
        clock_samples = _enrich_clock_samples(
            str(execution["time_mode"]),
            raw_clock_samples,
            metric_samples,
        )
        try:
            timing = evaluate_timing(execution, scenario["time_policy"], clock_samples)
        except TimingValidationError as error:
            timing = error.observation
            timing_failure = AssertionEvaluation(
                assertion_id="time-policy",
                status="failed",
                observed_value=None,
                unit="1",
                message=str(error),
            )
    assertions = list(
        evaluate_acceptance(
            EvaluationContext(
                run_id=run_id,
                domain_id=domain_id,
                bundle=bundle,
                evidence=evidence,
                metric_samples=metric_samples,
                window_start_ns=measurement_started_ns,
                window_end_ns=measurement_finished_ns,
            ),
            evaluator_receipts=evaluator_receipts,
        )
    )
    if timing_failure is not None:
        assertions.append(timing_failure)
    forbidden_observation: ForbiddenGraphObservation = forbidden_monitor.result()
    finished_at = utc_now()
    evidence_finalized = evidence.index.data.get("finalized") is True
    shutdown = {
        "observer_detached": True,
        "recorders_closed": measurement_complete.is_file() and evidence_finalized,
        "evidence_index_finalized": evidence_finalized,
    }
    monotonic_duration_sec = (
        measurement_finished_monotonic_ns - measurement_started_monotonic_ns
    ) / 1_000_000_000
    time_authority = evaluate_time_authority(
        scenario["time_policy"],
        metric_samples,
        run_id=run_id,
        domain_id=domain_id,
        source_id=str(run_context.data["time_authority"]["source_id"]),
        window_start_ns=measurement_started_ns,
        window_end_ns=measurement_finished_ns,
    )
    result = build_acceptance_result(
        result_id=result_id,
        run_id=run_id,
        domain_id=domain_id,
        bundle=bundle,
        readiness=readiness,
        timing=timing,
        time_authority=time_authority,
        time_authority_evidence_sha256=metrics_evidence_sha256,
        assertions=assertions,
        unevaluated=[],
        started_at=started_at,
        finished_at=finished_at,
        monotonic_duration_sec=monotonic_duration_sec,
        shutdown=shutdown,
        evidence_index=evidence,
        forbidden_graph=forbidden_observation,
        hardware_timing=hardware_timing,
        hardware_timing_evidence_sha256=(
            metrics_evidence_sha256 if hardware_timing is not None else None
        ),
    )
    destination = Path(output_dir).expanduser().resolve()
    result_path = write_contract_json(result, destination / "acceptance-result.json")
    junit_path = write_junit_xml(result, destination / "junit.xml")
    return VerificationOutputs(result, result_path, junit_path)


def evaluate_from_evidence(
    *,
    run_id: str,
    bundle: DocumentBundle,
    domain_id: str,
    run_context_path: str | Path,
    evidence_index_path: str | Path,
    artifact_receipt_paths: Sequence[str | Path] = (),
    artifact_verification_paths: Sequence[str | Path] = (),
    receipt_dependency_paths: Sequence[str | Path] = (),
    evaluator_receipts: VerifiedReceiptSet | None = None,
    otel_metrics_path: str | Path,
    window_start_ns: int,
    window_end_ns: int,
    output_dir: str | Path,
) -> VerificationOutputs:
    """Evaluate finalized playback evidence without attaching to a ROS graph."""

    if window_end_ns <= window_start_ns:
        raise VerificationError("offline evaluation window must have positive duration")
    run_context = load_run_context(
        run_context_path,
        run_id=run_id,
        domain_id=domain_id,
        scenario_id=str(bundle.scenario_data["scenario_id"]),
        scenario_sha256=bundle.scenario.sha256,
    )
    evidence = load_evidence_index(
        evidence_index_path,
        expected_run_id=run_id,
        receipt_paths=artifact_receipt_paths,
        verification_paths=artifact_verification_paths,
        receipt_dependency_paths=receipt_dependency_paths,
    )
    metrics_path = Path(otel_metrics_path).expanduser().resolve()
    metric_link = evidence.local_files.get(metrics_path)
    if metric_link is None or metric_link["media_type"] != OTLP_JSON_LINES_MEDIA_TYPE:
        raise VerificationError(
            f"OTLP metrics must be verified local {OTLP_JSON_LINES_MEDIA_TYPE} evidence"
        )
    metric_samples = _measurement_metrics(
        select_metric_points(
            load_otlp_json_metrics(
                metrics_path,
                expected_sha256=str(metric_link["sha256"]),
            ),
            run_id=run_id,
            domain_id=domain_id,
        ),
        window_start_ns=window_start_ns,
        window_end_ns=window_end_ns,
    )
    assertions = evaluate_acceptance(
        EvaluationContext(
            run_id=run_id,
            domain_id=domain_id,
            bundle=bundle,
            evidence=evidence,
            metric_samples=metric_samples,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        ),
        evaluator_receipts=evaluator_receipts,
    )
    time_authority = evaluate_time_authority(
        bundle.scenario_data["time_policy"],
        metric_samples,
        run_id=run_id,
        domain_id=domain_id,
        source_id=str(run_context.data["time_authority"]["source_id"]),
        window_start_ns=window_start_ns,
        window_end_ns=window_end_ns,
    )
    readiness = ReadinessResult(GraphSnapshot(window_end_ns), window_start_ns, 0.0)
    result = build_acceptance_result(
        result_id=f"result-{uuid4()}",
        run_id=run_id,
        domain_id=domain_id,
        bundle=bundle,
        readiness=readiness,
        timing=TimingObservation(False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        time_authority=time_authority,
        time_authority_evidence_sha256=str(metric_link["sha256"]),
        assertions=assertions,
        unevaluated=(
            "$.clock_observation",
            "$.forbidden_graph_observation",
            "$.observed_ros_graph",
            "$.shutdown",
        ),
        started_at=utc_datetime_from_unix_ns(window_start_ns),
        finished_at=utc_datetime_from_unix_ns(window_end_ns),
        monotonic_duration_sec=(window_end_ns - window_start_ns) / 1_000_000_000,
        shutdown={
            "observer_detached": True,
            "recorders_closed": True,
            "evidence_index_finalized": evidence.index.data.get("finalized") is True,
        },
        evidence_index=evidence,
        forbidden_graph=ForbiddenGraphObservation((), (), (), ()),
        evaluation_mode="offline",
    )
    destination = Path(output_dir).expanduser().resolve()
    result_path = write_contract_json(result, destination / "acceptance-result.json")
    junit_path = write_junit_xml(result, destination / "junit.xml")
    return VerificationOutputs(result, result_path, junit_path)

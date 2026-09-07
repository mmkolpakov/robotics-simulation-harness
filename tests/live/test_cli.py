from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep, time_ns
from uuid import uuid4
from xml.etree import ElementTree

import pytest
import yaml
from robotics_runtime_contracts import validate_document

from robotics_acceptance_harness.documents import load_bundle
from robotics_acceptance_harness.metrics import HistogramSample
from robotics_acceptance_harness.otel import load_otlp_json_metrics, select_metric_points
from tests.live.evidence import (
    QUALIFICATION_GOLDEN,
    QUALIFICATION_METRICS,
    clock_recording,
    qualification_metrics,
)
from tests.support import local_evidence_artifact, write_evidence_index

pytestmark = pytest.mark.live_ros
BASE = Path(__file__).parents[1] / "fixtures" / "simulation"
GOLDEN = Path(__file__).parent / "fixtures" / "metric-golden.json"


def _attributes(values: dict) -> list[dict]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def _payload(golden: dict, run_id: str, domain_id: str) -> dict:
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": _attributes(
                        {**golden["resource_attributes"], "run.id": run_id, "domain.id": domain_id}
                    )
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "harness-live-test"},
                        "metrics": [
                            {
                                "name": golden["name"],
                                "unit": golden["unit"],
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": str(time_ns()),
                                            "asDouble": golden["value"],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _write_bundle(graph: dict, live_output: Path, *, qualified: bool = False) -> tuple[Path, Path]:
    scenario = yaml.safe_load((BASE / "scenario.yaml").read_text(encoding="utf-8"))
    runtime = yaml.safe_load((BASE / "runtime.yaml").read_text(encoding="utf-8"))
    runtime["ros"]["domain_id"] = int(os.environ.get("ROS_DOMAIN_ID", "0"))
    scenario["expected_ros_graph"] = graph
    scenario["timeouts"].update(
        graph_ready_sec=20, stable_for_sec=0.3, execution_sec=2, shutdown_sec=20
    )
    scenario["execution"]["time_mode"] = "simulation_stepped"
    runtime["execution"]["time_mode"] = "simulation_stepped"
    scenario["time_policy"] = {
        "step_size_sec": 0.02,
        "max_skipped_steps": 100,
        "time_authority_min_samples": 30,
        "max_time_authority_delivery_latency_p50_ms": 100,
        "max_time_authority_delivery_latency_p95_ms": 100,
        "max_time_authority_delivery_latency_ms": 100,
    }
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    scenario["metric_definitions"] = [
        {
            "metric_name": golden["name"],
            "unit": golden["unit"],
            "instrument_kind": "gauge",
            "temporality": "instantaneous",
        }
    ]
    if qualified:
        scenario["metric_definitions"].extend(
            {
                "metric_name": name,
                "unit": unit,
                "instrument_kind": kind,
                "temporality": "delta",
                **({"monotonic": True} if kind == "sum" else {}),
            }
            for name, (unit, kind) in QUALIFICATION_METRICS.items()
        )
    scenario["assertions"] = [
        {
            "assertion_id": "collector-golden",
            "kind": "metric",
            "metric_name": golden["name"],
            "unit": golden["unit"],
            "aggregation": "mean",
            "operator": "eq",
            "threshold": golden["value"],
            "window_sec": 2,
            "attribute_match": golden["resource_attributes"],
        }
    ]
    scenario_path = live_output / "scenario.yaml"
    runtime_path = live_output / "runtime.yaml"
    scenario_path.write_text(yaml.safe_dump(scenario), encoding="utf-8")
    runtime_path.write_text(yaml.safe_dump(runtime), encoding="utf-8")
    load_bundle(scenario_path, runtime_path=runtime_path)
    return scenario_path, runtime_path


def _verify(live_graph, collector, live_output: Path, *, qualified: bool) -> dict:
    scenario_path, runtime_path = _write_bundle(
        live_graph.expected(), live_output, qualified=qualified
    )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    run_id = f"run-{uuid4()}"
    domain_id = "live-domain"
    run_context = live_output / "acceptance-run.json"
    cli = [sys.executable, "-m", "robotics_acceptance_harness.cli"]
    created = subprocess.run(
        [
            *cli,
            "create-run",
            "--scenario",
            str(scenario_path),
            "--run-id",
            run_id,
            "--domain",
            f"{domain_id}=simulator",
            "--time-authority",
            "sim_clock",
            "--time-source",
            "live-clock",
            "--output",
            str(run_context),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    marker = live_output / "measurement-complete"
    evidence = live_output / "evidence-index.yaml"
    command = [
        *cli,
        "verify",
        "--scenario",
        str(scenario_path),
        "--runtime",
        str(runtime_path),
        "--run-id",
        run_id,
        "--domain-id",
        domain_id,
        "--run-context",
        str(run_context),
        "--evidence-index",
        str(evidence),
        "--otel-metrics",
        str(collector.metrics),
        "--measurement-complete",
        str(marker),
        "--output",
        str(live_output),
    ]
    # Warm up the collector before starting the observation window.
    collector.send(_payload(golden, run_id, domain_id))
    with (
        (live_output / "verify.stdout").open("w+") as stdout,
        (live_output / "verify.stderr").open("w+") as stderr,
    ):
        fixture_start_ns = time_ns()
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        try:
            deadline = monotonic() + 35
            while not marker.exists() and process.poll() is None and monotonic() < deadline:
                collector.send(_payload(golden, run_id, domain_id))
                sleep(0.05)
            stdout.flush()
            stderr.flush()
            assert marker.exists(), (live_output / "verify.stderr").read_text()
            if qualified:
                payload = _payload(golden, run_id, domain_id)
                resource = payload["resourceMetrics"][0]
                resource["resource"]["attributes"].extend(
                    _attributes(
                        {
                            "channel": live_graph.topic,
                            "time.source.id": "live-clock",
                            "time.measurement.method": "rmw_source_to_reception_latency",
                        }
                    )
                )
                resource["scopeMetrics"][0]["metrics"] = qualification_metrics(
                    fixture_start_ns, time_ns()
                )
                # Synthetic qualification values are exported by the real Collector.
                collector.send(payload)
            # Stop and flush the real exporter before hashing or publishing the index.
            collector.stop()
            points = select_metric_points(
                load_otlp_json_metrics(collector.metrics), run_id=run_id, domain_id=domain_id
            )
            gauges = [p for p in points if p.name == golden["name"]]
            assert len(gauges) >= 10
            assert {(p.name, p.unit, p.value) for p in gauges} == {
                (golden["name"], golden["unit"], golden["value"])
            }
            assert all(
                all(p.attributes.get(k) == v for k, v in golden["resource_attributes"].items())
                for p in points
            )
            assert {p.name for p in points} == (
                {golden["name"], *QUALIFICATION_METRICS} if qualified else {golden["name"]}
            )
            for point in points:
                if point.name not in QUALIFICATION_METRICS:
                    continue
                assert point.temporality == "delta"
                assert (
                    point.observed_at_ns - point.start_time_ns
                    == QUALIFICATION_GOLDEN["interval_ns"]
                )
                if isinstance(point, HistogramSample):
                    key = (
                        "message_age_ms"
                        if point.name == "robotics.message.age"
                        else "delivery_latency_ms"
                    )
                    value = QUALIFICATION_GOLDEN[key]
                    assert point.count == QUALIFICATION_GOLDEN["histogram_count"]
                    assert point.min == point.max == value
                    assert point.sum == pytest.approx(value * point.count)
                    assert point.explicit_bounds == tuple(
                        QUALIFICATION_GOLDEN["explicit_bounds_ms"]
                    )
                else:
                    assert point.monotonic
                    assert point.value == QUALIFICATION_GOLDEN["counters"][point.name]
            artifact = local_evidence_artifact(collector.metrics, media_type="application/x-ndjson")
            artifacts = [artifact]
            if qualified:
                artifacts.append(
                    clock_recording(live_output, tuple(live_graph.recorded_clock_samples[-100:]))
                )
            pending = live_output / "evidence-pending.yaml"
            write_evidence_index(pending, run_id=run_id, artifacts=artifacts)
            pending.replace(evidence)
            exit_code = process.wait(timeout=25)
            assert exit_code == (0 if qualified else 1), (
                live_output / "verify.stdout"
            ).read_text() + (live_output / "verify.stderr").read_text()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    result = json.loads((live_output / "acceptance-result.json").read_text(encoding="utf-8"))
    validate_document(result)
    assert result["evaluation_mode"] == "live"
    assertions = {item["assertion_id"]: item for item in result["assertion_results"]}
    assert assertions["collector-golden"]["status"] == "passed"
    assert assertions["collector-golden"]["observed_value"] == golden["value"]
    assert (
        result["time_authority_observation"]["evidence_sha256"]
        == sha256(collector.metrics.read_bytes()).hexdigest()
    )
    graph = result["observed_ros_graph"]
    assert graph["topics"][0]["name"] == live_graph.topic
    assert graph["topics"][0]["publishers"] == 1
    assert graph["topics"][0]["subscribers"] == 1
    assert graph["services"][0]["name"] == live_graph.service
    assert graph["services"][0]["server_nodes"] == 1
    assert graph["actions"][0]["name"] == live_graph.action
    assert graph["actions"][0]["server_nodes"] == 1
    assert result["lifecycle_states"][0]["state"] == "active"
    assert result["clock_observation"]["monotonic"] is True
    assert all(result["shutdown"].values())
    assert not any(item["assertion_id"] == "time-policy" for item in result["assertion_results"])
    junit = ElementTree.parse(live_output / "junit.xml")
    assert junit.findall(".//testcase")
    if qualified:
        assert not junit.findall(".//error")
        assert not junit.findall(".//failure")
        assert not junit.findall(".//skipped")
    else:
        assert junit.findall(".//error") or junit.findall(".//failure")
    return result


def test_verify_passes_with_complete_evidence(live_graph, collector, live_output: Path) -> None:
    result = _verify(live_graph, collector, live_output, qualified=True)
    assert result["status"] == "passed"
    assert result["unevaluated"] == []
    assert all(item["status"] == "passed" for item in result["assertion_results"])
    authority = result["time_authority_observation"]
    assert authority["within_policy"] is True
    assert authority["sample_count"] >= 30
    assertions = {item["assertion_id"]: item for item in result["assertion_results"]}
    assert assertions["data-plane-loss-ratio"]["observed_value"] == 0
    assert assertions["data-plane-sequence-integrity"]["observed_value"] == 0
    assert assertions["policy-evidence-topics"]["status"] == "passed"
    assert assertions["policy-evidence-compression"]["status"] == "passed"


def test_verify_rejects_missing_evidence(live_graph, collector, live_output: Path) -> None:
    result = _verify(live_graph, collector, live_output, qualified=False)
    # Assert rejection and its evidence causes, not the current status-folding precedence.
    assert result["status"] != "passed"
    assertions = {item["assertion_id"]: item for item in result["assertion_results"]}
    assert assertions["data-plane-message-age"]["status"] != "passed"
    assert assertions["policy-evidence-topics"]["status"] == "failed"
    assert result["time_authority_observation"]["within_policy"] is False

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from robotics_acceptance_harness.cli import main
from robotics_acceptance_harness.hardware_timing import HardwareTimingObservation
from tests.support import write_extended_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "simulation"


def test_explain_validates_bundle_without_ros(capsys) -> None:
    exit_code = main(
        [
            "explain",
            "--scenario",
            str(FIXTURES / "scenario.yaml"),
            "--runtime",
            str(FIXTURES / "runtime.yaml"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["policy"] == "accepted-simulation"
    assert output["workload_kind"] == "none"
    assert output["unevaluated"] == []


def test_create_run_derives_identity_and_digest_from_scenario(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = "run-6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    output_path = tmp_path / "acceptance-run.json"

    exit_code = main(
        [
            "create-run",
            "--scenario",
            str(FIXTURES / "scenario.yaml"),
            "--output",
            str(output_path),
            "--domain",
            "primary=observer",
            "--time-authority",
            "sim_clock",
            "--time-source",
            "gazebo-clock",
            "--run-id",
            run_id,
        ]
    )

    document = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == run_id
    assert document["scenario_id"] == "org.example.physics-smoke"
    assert document["scenario_sha256"]
    assert document["domains"] == [{"domain_id": "primary", "role": "observer"}]


def test_explain_rejects_invalid_extension_argument(capsys) -> None:
    exit_code = main(
        [
            "explain",
            "--scenario",
            str(FIXTURES / "scenario.yaml"),
            "--runtime",
            str(FIXTURES / "runtime.yaml"),
            "--extension-schema",
            "invalid",
        ]
    )

    assert exit_code == 2
    assert "invalid --extension-schema" in capsys.readouterr().err


def test_explain_loads_extension_schema_by_canonical_uri(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario, schema, uri = write_extended_scenario(tmp_path, FIXTURES / "scenario.yaml")

    exit_code = main(
        [
            "explain",
            "--scenario",
            str(scenario),
            "--runtime",
            str(FIXTURES / "runtime.yaml"),
            "--extension-schema",
            f"{uri}={schema}",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["policy"] == "accepted-simulation"


def test_create_run_loads_extension_schema_by_canonical_uri(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario, schema, uri = write_extended_scenario(tmp_path, FIXTURES / "scenario.yaml")
    output = tmp_path / "acceptance-run.json"

    exit_code = main(
        [
            "create-run",
            "--scenario",
            str(scenario),
            "--extension-schema",
            f"{uri}={schema}",
            "--output",
            str(output),
            "--domain",
            "primary=observer",
            "--time-authority",
            "sim_clock",
            "--time-source",
            "gazebo-clock",
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["scenario_sha256"]
    assert capsys.readouterr().out.startswith("run-")


def test_verify_requires_run_id(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "verify",
                "--scenario",
                str(FIXTURES / "scenario.yaml"),
                "--runtime",
                str(FIXTURES / "runtime.yaml"),
                "--evidence-index",
                "evidence-index.yaml",
                "--output",
                "output",
            ]
        )

    assert caught.value.code == 2
    assert "--run-id" in capsys.readouterr().err


def test_verify_forwards_canonical_run_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run_verification(**arguments: object) -> SimpleNamespace:
        captured.update(arguments)
        return SimpleNamespace(
            result={"status": "passed"},
            result_path=tmp_path / "acceptance-result.json",
            junit_path=tmp_path / "junit.xml",
        )

    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.run_verification",
        fake_run_verification,
    )

    exit_code = main(
        [
            "verify",
            "--scenario",
            str(FIXTURES / "scenario.yaml"),
            "--runtime",
            str(FIXTURES / "runtime.yaml"),
            "--run-id",
            "run-6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            "--domain-id",
            "camera-domain",
            "--run-context",
            "acceptance-run.yaml",
            "--evidence-index",
            "evidence-index.yaml",
            "--otel-metrics",
            "metrics.otlp.json",
            "--measurement-complete",
            str(tmp_path / "measurement-complete"),
            "--output",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert captured["domain_id"] == "camera-domain"
    assert captured["run_context_path"] == "acceptance-run.yaml"
    assert captured["otel_metrics_path"] == "metrics.otlp.json"
    assert captured["measurement_complete_path"] == str(tmp_path / "measurement-complete")
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_aggregate_forwards_transport_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    output = tmp_path / "acceptance-aggregate.json"

    def fake_aggregate_results(**arguments: object) -> Path:
        captured.update(arguments)
        output.write_text(
            json.dumps(
                {
                    "per_domain_aggregate": "passed",
                    "cross_domain_e2e": {"status": "failed"},
                }
            ),
            encoding="utf-8",
        )
        return output

    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.aggregate_results",
        fake_aggregate_results,
    )

    exit_code = main(
        [
            "aggregate",
            "--scenario",
            "scenario.yaml",
            "--run-context",
            "acceptance-run.json",
            "--result",
            "domain-result.json",
            "--transport-qualification",
            "transport-qualification.json",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert captured["scenario_path"] == "scenario.yaml"
    assert captured["transport_qualification_path"] == "transport-qualification.json"
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_transport_evaluate_maps_domain_evidence_and_reports_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    output = tmp_path / "transport-qualification.json"

    def fake_evaluate_transport_qualification(**arguments: object) -> Path:
        captured.update(arguments)
        output.write_text(
            json.dumps({"verdict": {"status": "passed"}}),
            encoding="utf-8",
        )
        return output

    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.evaluate_transport_qualification",
        fake_evaluate_transport_qualification,
    )

    exit_code = main(
        [
            "transport-evaluate",
            "--run-id",
            "run-6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            "--scenario",
            "scenario.yaml",
            "--causal-chain",
            "causal-chain.json",
            "--channel-contract",
            "channel.json",
            "--trace",
            "source=source-traces.json",
            "--trace",
            "target=target-traces.json",
            "--evidence-index",
            "source=source-evidence.json",
            "--evidence-index",
            "target=target-evidence.json",
            "--clock-relation",
            "clock-relation.json",
            "--observation-output",
            str(tmp_path / "observations"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert captured["trace_paths"] == {
        "source": "source-traces.json",
        "target": "target-traces.json",
    }
    assert captured["evidence_index_paths"] == {
        "source": "source-evidence.json",
        "target": "target-evidence.json",
    }
    assert captured["clock_relation_paths"] == ["clock-relation.json"]
    assert captured["scenario_path"] == "scenario.yaml"
    assert json.loads(capsys.readouterr().out) == {
        "qualification": str(output),
        "status": "passed",
    }


def test_doctor_reports_extension_inventory(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["doctor"])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["status"] == "passed"
    assert isinstance(report["evaluators"], list)


def test_doctor_fails_for_a_stale_measurement_marker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "measurement-complete"
    marker.touch()

    exit_code = main(["doctor", "--measurement-complete", str(marker)])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["status"] == "failed"
    assert any(item["check_id"] == "measurement-marker" for item in report["checks"])


def test_otel_summary_uses_the_public_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.load_otlp_json_metrics",
        lambda _path: (
            SimpleNamespace(name="robotics.clock.offset"),
            SimpleNamespace(name="robotics.clock.offset"),
        ),
    )

    exit_code = main(["otel-summary", "--otel-metrics", "metrics.jsonl"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "sample_count": 2,
        "instruments": {"robotics.clock.offset": 2},
    }


def test_otel_summary_reports_invalid_protobuf_as_input_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "bad.otlp.jsonl"
    source.write_text('{"resourceMetrics": "invalid"}\n', encoding="utf-8")

    assert main(["otel-summary", "--otel-metrics", str(source)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad.otlp.jsonl:1:" in captured.err
    assert "Traceback" not in captured.err


def test_timing_check_exposes_policy_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observation = HardwareTimingObservation(
        sync_protocol="ptp",
        source="pmc",
        measured_at=datetime(2026, 8, 17, tzinfo=UTC),
        sample_count=4,
        offset_ms=0.5,
        jitter_ms=0.1,
        drift_ppm=1,
        max_sample_age_ms=2,
        monotonic=True,
        within_policy=False,
    )
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.load_document",
        lambda *_args, **_kwargs: SimpleNamespace(
            data={"time_policy": {}, "scenario_id": "org.example.timing"},
            sha256="b" * 64,
        ),
    )
    run_context: dict[str, object] = {}
    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.load_run_context",
        lambda path, **kwargs: run_context.update(path=path, **kwargs),
    )
    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.load_evidence_index",
        lambda *_args, **_kwargs: SimpleNamespace(
            local_files={
                metrics_path.resolve(): {
                    "media_type": "application/x-ndjson",
                    "sha256": "a" * 64,
                }
            }
        ),
    )
    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.load_otlp_json_metrics",
        lambda _path, **_kwargs: (),
    )
    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.evaluate_hardware_timing",
        lambda _policy, _samples: observation,
    )

    exit_code = main(
        [
            "timing-check",
            "--scenario",
            "scenario.yaml",
            "--run-context",
            "acceptance-run.json",
            "--run-id",
            "run-01234567-89ab-4def-8123-456789abcdef",
            "--domain-id",
            "primary",
            "--evidence-index",
            "evidence-index.json",
            "--otel-metrics",
            str(metrics_path),
            "--expect",
            "out-of-policy",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["within_policy"] is False
    assert report["measured_at"] == "2026-08-17T00:00:00Z"
    assert run_context == {
        "path": "acceptance-run.json",
        "run_id": "run-01234567-89ab-4def-8123-456789abcdef",
        "domain_id": "primary",
        "scenario_id": "org.example.timing",
        "scenario_sha256": "b" * 64,
    }


def test_evaluate_forwards_offline_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_evaluate_from_evidence(**arguments: object) -> SimpleNamespace:
        captured.update(arguments)
        return SimpleNamespace(
            result={"status": "incomplete"},
            result_path=tmp_path / "acceptance-result.json",
            junit_path=tmp_path / "junit.xml",
        )

    monkeypatch.setattr(
        "robotics_acceptance_harness.cli.evaluate_from_evidence",
        fake_evaluate_from_evidence,
    )
    exit_code = main(
        [
            "evaluate",
            "--scenario",
            str(FIXTURES / "scenario.yaml"),
            "--runtime",
            str(FIXTURES / "runtime.yaml"),
            "--run-id",
            "run-6ba7b810-9dad-41d1-80b4-00c04fd430c8",
            "--domain-id",
            "primary",
            "--run-context",
            "run.json",
            "--evidence-index",
            "evidence.json",
            "--otel-metrics",
            "metrics.json",
            "--window-start-ns",
            "100",
            "--window-end-ns",
            "200",
            "--output",
            str(tmp_path),
        ]
    )

    assert exit_code == 1
    assert captured["window_start_ns"] == 100
    assert captured["window_end_ns"] == 200
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"

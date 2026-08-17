from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from robotics_acceptance_harness.diagnostics import doctor_report, why_report
from robotics_acceptance_harness.result import build_acceptance_result, write_contract_json
from robotics_acceptance_harness.time_authority import TimeAuthorityObservation
from tests.test_result import result_inputs


def test_doctor_checks_every_live_ros_dependency(monkeypatch) -> None:
    def import_module(name: str) -> object:
        if name == "rclpy":
            return object()
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(
        "robotics_acceptance_harness.diagnostics.import_module",
        import_module,
    )

    report = doctor_report(mode="live")

    assert report["status"] == "failed"
    failed = {item["check_id"] for item in report["checks"] if item["status"] == "failed"}
    assert "python-module-rosidl-runtime-py" in failed
    assert "python-module-lifecycle-msgs" in failed
    assert "python-module-rosgraph-msgs" in failed


def test_doctor_rejects_module_with_broken_native_import(monkeypatch) -> None:
    def import_module(name: str) -> object:
        if name == "rclpy":
            raise ImportError("native extension is unavailable")
        return object()

    monkeypatch.setattr(
        "robotics_acceptance_harness.diagnostics.import_module",
        import_module,
    )

    report = doctor_report(mode="live")

    check = next(item for item in report["checks"] if item["check_id"] == "python-module-rclpy")
    assert check["status"] == "failed"
    assert "native extension is unavailable" in check["message"]


def test_doctor_does_not_import_evaluator_targets(monkeypatch) -> None:
    entry_point = EntryPoint(
        "org.example.broken",
        "missing_evaluator_package:evaluate",
        "robotics_acceptance.evaluators",
    )
    monkeypatch.setattr(
        "robotics_acceptance_harness.evaluation.entry_points",
        lambda **_kwargs: (entry_point,),
    )

    report = doctor_report()

    discovery = next(item for item in report["checks"] if item["check_id"] == "evaluator-metadata")
    assert discovery["status"] == "failed"
    assert report["evaluators"][0]["target"] == "missing_evaluator_package:evaluate"


def test_why_reports_failed_runtime_observations(tmp_path: Path) -> None:
    inputs = result_inputs(tmp_path)
    inputs["time_authority"] = TimeAuthorityObservation(
        source_id="simulation-clock",
        sample_count=30,
        window_start_ns=0,
        window_end_ns=1,
        p50_ms=1,
        p95_ms=2,
        max_ms=10,
        within_policy=False,
    )
    result = build_acceptance_result(**inputs)
    path = write_contract_json(result, tmp_path / "result.json")

    report = why_report(path)

    assert report["runtime_observations"] == [
        {
            "observation_id": "time-authority-policy",
            "status": "failed",
            "message": "time-authority evidence is out of policy",
        }
    ]

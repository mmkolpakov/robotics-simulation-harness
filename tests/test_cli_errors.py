from __future__ import annotations

import json
from pathlib import Path

import pytest

from robotics_acceptance_harness.cli import main


@pytest.mark.parametrize("error", [KeyError("foreign domain"), AttributeError("missing API")])
def test_unexpected_command_errors_are_diagnostic_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    def fail(**_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("robotics_acceptance_harness.cli.doctor_report", fail)
    diagnostic = tmp_path / "diagnostic.json"

    assert main(["doctor", "--diagnostic-output", str(diagnostic)]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert str(error) in output.err
    assert "internal.error" in output.err
    assert "Traceback" not in output.err
    assert json.loads(diagnostic.read_text(encoding="utf-8")) == {
        "command": "doctor",
        "status": "error",
        "error_id": "internal.error",
        "exception_type": type(error).__name__,
        "message": str(error),
    }


def test_diagnostic_write_failure_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs: object) -> None:
        raise KeyError("original failure")

    monkeypatch.setattr("robotics_acceptance_harness.cli.doctor_report", fail)

    assert main(["doctor", "--diagnostic-output", str(tmp_path)]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert "original failure" in output.err
    assert "cannot write diagnostic" in output.err
    assert "Traceback" not in output.err


def test_malformed_otlp_can_write_a_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "bad.otlp.jsonl"
    source.write_text('{"unknownField": true}\n', encoding="utf-8")
    diagnostic = tmp_path / "diagnostic.json"

    assert (
        main(
            [
                "otel-summary",
                "--otel-metrics",
                str(source),
                "--diagnostic-output",
                str(diagnostic),
            ]
        )
        == 2
    )

    report = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert report["error_id"] == "MetricInputError.failed"
    assert "bad.otlp.jsonl:1:" in report["message"]
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7)])
def test_process_control_exceptions_propagate(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def fail(**_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("robotics_acceptance_harness.cli.doctor_report", fail)

    with pytest.raises(type(error)) as caught:
        main(["doctor"])
    assert caught.value is error

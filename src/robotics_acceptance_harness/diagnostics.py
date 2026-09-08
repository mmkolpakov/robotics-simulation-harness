from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping, Sequence
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from typing import Any

from robotics_acceptance_harness import __version__
from robotics_acceptance_harness.documents import load_document
from robotics_acceptance_harness.evaluation import evaluator_inventory
from robotics_acceptance_harness.receipts import VerifiedReceiptSet


def _check(check_id: str, passed: bool, message: str) -> dict[str, str]:
    return {
        "check_id": check_id,
        "status": "passed" if passed else "failed",
        "message": message,
    }


def _module_import_error(name: str) -> str | None:
    try:
        import_module(name)
    except (ImportError, OSError) as error:
        return f"{type(error).__name__}: {error}"
    return None


def doctor_report(
    *,
    mode: str = "offline",
    evidence_dir: str | Path | None = None,
    measurement_complete: str | Path | None = None,
    evaluator_requirements: Sequence[Mapping[str, Any]] = (),
    evaluator_receipts: VerifiedReceiptSet | None = None,
) -> dict[str, Any]:
    """Check dependencies required by the selected evaluation mode."""

    if mode not in {"live", "offline"}:
        raise ValueError(f"unsupported doctor mode: {mode}")
    checks = [_check("contracts-package", True, "robotics-runtime-contracts is importable")]
    try:
        inventory = list(evaluator_inventory(evaluator_requirements, evaluator_receipts))
        namespaces = [str(item["namespace"]) for item in inventory]
        metadata_ready = len(namespaces) == len(set(namespaces)) and all(
            item["distribution"] != "unknown" and item["version"] != "unknown" for item in inventory
        )
        expected_count = len(evaluator_requirements)
        qualified = not evaluator_requirements or len(inventory) == expected_count
        checks.append(
            _check(
                "evaluator-metadata",
                metadata_ready and qualified,
                (
                    f"qualified {len(inventory)} required evaluator(s)"
                    if evaluator_requirements
                    else f"discovered {len(inventory)} evaluator(s) with valid metadata"
                ),
            )
        )
    except ValueError as error:
        inventory = []
        checks.append(_check("evaluator-metadata", False, str(error)))
    if mode == "live":
        for module in ("rclpy", "rosidl_runtime_py", "lifecycle_msgs", "rosgraph_msgs"):
            import_error = _module_import_error(module)
            checks.append(
                _check(
                    f"python-module-{module.replace('_', '-')}",
                    import_error is None,
                    (
                        f"{module} imported successfully"
                        if import_error is None
                        else f"{module} import failed: {import_error}"
                    ),
                )
            )
        domain = os.environ.get("ROS_DOMAIN_ID", "0")
        try:
            domain_valid = 0 <= int(domain) <= 232
        except ValueError:
            domain_valid = False
        checks.append(
            _check(
                "ros-domain-id",
                domain_valid,
                f"ROS_DOMAIN_ID={domain!r} must be an integer from 0 through 232",
            )
        )
    if evidence_dir is not None:
        directory = Path(evidence_dir).expanduser().resolve()
        checks.append(
            _check(
                "evidence-directory",
                directory.is_dir() and os.access(directory, os.W_OK),
                f"evidence directory must exist and be writable: {directory}",
            )
        )
    if measurement_complete is not None:
        marker = Path(measurement_complete).expanduser().resolve()
        checks.append(
            _check(
                "measurement-marker",
                not marker.exists(),
                f"stale measurement marker must be absent: {marker}",
            )
        )
    return {
        "status": ("passed" if all(item["status"] == "passed" for item in checks) else "failed"),
        "mode": mode,
        "python": platform.python_version(),
        "robotics_acceptance_harness": __version__,
        "robotics_runtime_contracts": version("robotics-runtime-contracts"),
        "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION", "default"),
        "evaluators": inventory,
        "checks": checks,
    }


def why_report(path: str | Path) -> dict[str, Any]:
    """Extract actionable verdict causes from a canonical result."""

    result = load_document(
        path,
        expected_role="acceptance_result",
    )
    assertions = [
        {
            "assertion_id": item["assertion_id"],
            "status": item["status"],
            "message": item.get("message", ""),
        }
        for item in result.data["assertion_results"]
        if item["status"] != "passed"
    ]
    unevaluated = set(result.data["unevaluated"])
    observations: list[dict[str, str]] = []
    time_authority = result.data["time_authority_observation"]
    if "$.time_authority_observation" not in unevaluated and not time_authority["within_policy"]:
        observations.append(
            {
                "observation_id": "time-authority-policy",
                "status": "failed",
                "message": "time-authority evidence is out of policy",
            }
        )
    hardware = result.data.get("hardware_clock_observation")
    if hardware is not None and not hardware["within_policy"]:
        observations.append(
            {
                "observation_id": "hardware-clock-policy",
                "status": "failed",
                "message": "hardware timing is out of policy",
            }
        )
    if (
        "$.clock_observation" not in unevaluated
        and not result.data["clock_observation"]["monotonic"]
    ):
        observations.append(
            {
                "observation_id": "clock-monotonicity",
                "status": "failed",
                "message": "observed clock is not monotonic",
            }
        )
    if "$.shutdown" not in unevaluated:
        for field, passed in result.data["shutdown"].items():
            if not passed:
                observations.append(
                    {
                        "observation_id": f"shutdown.{field}",
                        "status": "failed",
                        "message": "shutdown condition was not satisfied",
                    }
                )
    return {
        "result_id": result.data["result_id"],
        "status": result.data["status"],
        "unevaluated": list(result.data["unevaluated"]),
        "assertions": assertions,
        "runtime_observations": observations,
        "forbidden_graph_violations": [
            dict(item) for item in result.data["forbidden_graph_observation"]["violations"]
        ],
    }


def report_markdown(report: Mapping[str, Any]) -> str:
    """Render a compact report suitable for a CI job summary."""

    lines = [f"# Acceptance verdict: {report['status']}"]
    if report.get("result_id"):
        lines.append(f"\nResult: `{report['result_id']}`")
    unevaluated = report.get("unevaluated", [])
    if unevaluated:
        lines.extend(("\n## Unevaluated", *(f"- `{item}`" for item in unevaluated)))
    assertions = report.get("assertions", [])
    if assertions:
        lines.append("\n## Assertions")
        lines.extend(
            f"- `{item['assertion_id']}`: **{item['status']}**"
            + (f" - {item['message']}" if item.get("message") else "")
            for item in assertions
        )
    violations = report.get("forbidden_graph_violations", [])
    if violations:
        lines.append("\n## Forbidden ROS graph")
        lines.extend(f"- `{item['kind']}` `{item['name']}`" for item in violations)
    observations = report.get("runtime_observations", [])
    if observations:
        lines.append("\n## Runtime observations")
        lines.extend(
            f"- `{item['observation_id']}`: **{item['status']}** - {item['message']}"
            for item in observations
        )
    checks = report.get("checks", [])
    if checks:
        lines.append("\n## Checks")
        lines.extend(
            f"- `{item['check_id']}`: **{item['status']}** - {item['message']}" for item in checks
        )
    return "\n".join(lines) + "\n"


def write_error_diagnostic(
    path: str | Path, *, command: str, error: Exception, error_id: str | None = None
) -> Path:
    """Write a stable machine-readable diagnostic after a command failure."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "error",
        "command": command,
        "error_id": error_id or getattr(error, "error_id", f"{type(error).__name__}.failed"),
        "exception_type": type(error).__name__,
        "message": str(error),
    }
    issues = getattr(error, "issues", ())
    if issues:
        payload["issues"] = [
            {
                "json_path": str(getattr(issue, "json_path", "")),
                "message": str(getattr(issue, "message", issue)),
            }
            for issue in issues
        ]
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return destination


__all__ = ["doctor_report", "report_markdown", "why_report", "write_error_diagnostic"]

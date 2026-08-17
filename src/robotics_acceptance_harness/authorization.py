from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AuthorizationIssue:
    json_path: str
    message: str


def _equal(
    issues: list[AuthorizationIssue],
    path: str,
    expected: Any,
    actual: Any,
) -> None:
    if expected != actual:
        issues.append(AuthorizationIssue(path, f"expected {expected!r}; received {actual!r}"))


def _timestamp(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def evaluate_physical_authorization(
    *,
    scenario: Mapping[str, Any],
    scenario_sha256: str,
    runtime: Mapping[str, Any],
    permit: Mapping[str, Any] | None,
    permit_sha256: str | None,
    permit_path: Path | None,
    verification: Mapping[str, Any] | None,
    verification_sha256: str | None,
    verification_path: Path | None,
    now: datetime,
) -> tuple[AuthorizationIssue, ...]:
    """Cross-check authorization facts without performing cryptography or I/O."""

    issues: list[AuthorizationIssue] = []
    target_environment = scenario["execution"]["target_environment"]
    physical = target_environment in {"hil", "real_robot"}
    if not physical:
        if permit is not None:
            issues.append(AuthorizationIssue("$.permit", "simulation must not carry a permit"))
        if verification is not None:
            issues.append(
                AuthorizationIssue("$.verification", "simulation must not carry verification")
            )
        return tuple(issues)

    if permit is None or permit_sha256 is None or permit_path is None:
        issues.append(AuthorizationIssue("$.permit", "physical observation requires a permit"))
    if verification is None or verification_sha256 is None or verification_path is None:
        issues.append(
            AuthorizationIssue(
                "$.verification",
                "physical observation requires execution verification",
            )
        )
    if issues:
        return tuple(issues)

    assert permit is not None
    assert permit_sha256 is not None
    assert permit_path is not None
    assert verification is not None
    assert verification_sha256 is not None
    assert verification_path is not None

    if permit_path.suffix.lower() != ".json":
        issues.append(AuthorizationIssue("$.permit", "physical permit must be UTF-8 JSON"))
    if verification_path.suffix.lower() != ".json":
        issues.append(
            AuthorizationIssue("$.verification", "execution verification must be UTF-8 JSON")
        )

    scenario_authorization = scenario["authorization"]
    runtime_authorization = runtime["authorization"]
    _equal(
        issues,
        "$.permit.scenario_sha256",
        scenario_sha256,
        permit["scenario_sha256"],
    )
    _equal(
        issues,
        "$.permit.subject_digest",
        runtime["execution_subject"]["digest"],
        permit["subject_digest"],
    )
    _equal(
        issues,
        "$.permit.trust_policy_sha256",
        scenario_authorization["trust_policy_sha256"],
        permit["trust_policy_sha256"],
    )
    _equal(
        issues,
        "$.verification.trust_policy_sha256",
        scenario_authorization["trust_policy_sha256"],
        verification["trust_policy_sha256"],
    )
    _equal(
        issues,
        "$.runtime.authorization.trust_policy_sha256",
        scenario_authorization["trust_policy_sha256"],
        runtime_authorization["trust_policy_sha256"],
    )
    _equal(
        issues,
        "$.verification.permit_sha256",
        permit_sha256,
        verification["permit_sha256"],
    )
    _equal(
        issues,
        "$.runtime.authorization.permit_sha256",
        permit_sha256,
        runtime_authorization["permit_sha256"],
    )
    _equal(
        issues,
        "$.runtime.authorization.execution_verification_sha256",
        verification_sha256,
        runtime_authorization["execution_verification_sha256"],
    )

    permit_target = permit["target"]
    verification_target = verification["target"]
    _equal(
        issues,
        "$.permit.target.environment",
        target_environment,
        permit_target["environment"],
    )
    for field in ("environment", "target_id", "identity_kind", "identity_sha256"):
        _equal(
            issues,
            f"$.verification.target.{field}",
            permit_target[field],
            verification_target[field],
        )

    runtime_targets = {str(target["target_id"]): target for target in runtime["physical_targets"]}
    runtime_target = runtime_targets.get(str(permit_target["target_id"]))
    if runtime_target is None:
        issues.append(
            AuthorizationIssue(
                "$.permit.target.target_id",
                "permit target is absent from runtime physical_targets",
            )
        )
    else:
        for field in ("identity_kind", "identity_sha256"):
            _equal(
                issues,
                f"$.runtime.physical_targets.{permit_target['target_id']}.{field}",
                permit_target[field],
                runtime_target[field],
            )

    scenario_scope = set(scenario["execution"]["hardware_scope"])
    permit_scope = set(permit["hardware_scope"])
    runtime_scope = {target["scope"] for target in runtime["physical_targets"]}
    if not scenario_scope.issubset(permit_scope):
        issues.append(
            AuthorizationIssue(
                "$.permit.hardware_scope",
                "does not cover scenario hardware_scope",
            )
        )
    if not scenario_scope.issubset(runtime_scope):
        issues.append(
            AuthorizationIssue(
                "$.runtime.physical_targets",
                "does not cover scenario hardware_scope",
            )
        )
    _equal(
        issues,
        "$.permit.allowed_physical_effect",
        scenario["execution"]["physical_effect"],
        permit["allowed_physical_effect"],
    )

    signers = {str(signer["role"]): signer for signer in verification["signers"]}
    _equal(
        issues,
        "$.verification.signers.operator.identity",
        permit["operator_id"],
        signers["operator"]["identity"],
    )
    _equal(
        issues,
        "$.verification.signers.approver.identity",
        permit["approver_id"],
        signers["approver"]["identity"],
    )

    issued_at = _timestamp(permit["issued_at"])
    expires_at = _timestamp(permit["expires_at"])
    verified_at = _timestamp(verification["verified_at"])
    if not issued_at <= now < expires_at:
        issues.append(
            AuthorizationIssue("$.permit.expires_at", "permit is not active at verification time")
        )
    if not issued_at <= verified_at < expires_at:
        issues.append(
            AuthorizationIssue(
                "$.verification.verified_at",
                "must fall inside the permit validity interval",
            )
        )
    if verified_at > now:
        issues.append(AuthorizationIssue("$.verification.verified_at", "must not be in the future"))
    return tuple(issues)

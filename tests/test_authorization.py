from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from robotics_acceptance_harness.documents import BundleValidationError, load_bundle

FIXTURES = Path(__file__).parent / "fixtures" / "physical"
NOW = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def _valid_bundle(**overrides: Any) -> Any:
    arguments = {
        "runtime_path": FIXTURES / "hil-runtime.json",
        "permit_path": FIXTURES / "hil-permit.json",
        "verification_path": FIXTURES / "hil-verification.json",
        "now": NOW,
    }
    arguments.update(overrides)
    return load_bundle(FIXTURES / "hil-scenario.yaml", **arguments)


def _write_json(path: Path, document: dict[str, Any]) -> str:
    raw = (json.dumps(document, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def _mutated_verification_bundle(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> Any:
    verification = json.loads((FIXTURES / "hil-verification.json").read_bytes())
    mutation(verification)
    verification_path = tmp_path / "verification.json"
    verification_sha256 = _write_json(verification_path, verification)

    runtime = json.loads((FIXTURES / "hil-runtime.json").read_bytes())
    runtime["authorization"]["execution_verification_sha256"] = verification_sha256
    runtime_path = tmp_path / "runtime.json"
    _write_json(runtime_path, runtime)
    return _valid_bundle(runtime_path=runtime_path, verification_path=verification_path)


def test_load_bundle_aligns_physical_authorization() -> None:
    bundle = _valid_bundle()

    assert bundle.scenario.schema_version == "acceptance-scenario.v1"
    assert bundle.runtime.schema_version == "runtime-manifest.v1"
    assert bundle.permit is not None
    assert bundle.permit.schema_version == "execution-permit.v1"
    assert bundle.verification is not None
    assert bundle.verification.schema_version == "execution-verification.v1"


def test_physical_bundle_requires_execution_verification() -> None:
    with pytest.raises(BundleValidationError, match="requires execution verification"):
        _valid_bundle(verification_path=None)


def test_physical_bundle_reports_all_missing_authorization_documents() -> None:
    with pytest.raises(BundleValidationError) as caught:
        load_bundle(
            FIXTURES / "hil-scenario.yaml",
            runtime_path=FIXTURES / "hil-runtime.json",
            now=NOW,
        )

    assert caught.value.issues == (
        ("$.permit", "physical observation requires a permit"),
        ("$.verification", "physical observation requires execution verification"),
    )


def test_operational_permit_must_be_json(tmp_path: Path) -> None:
    permit_path = tmp_path / "permit.yaml"
    permit_path.write_bytes((FIXTURES / "hil-permit.json").read_bytes())

    with pytest.raises(BundleValidationError, match="must be UTF-8 JSON"):
        _valid_bundle(permit_path=permit_path)


def test_permit_must_be_active_when_bundle_is_loaded() -> None:
    with pytest.raises(BundleValidationError, match="not active"):
        _valid_bundle(now=datetime(2026, 7, 12, 10, 11, tzinfo=UTC))


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        pytest.param(
            lambda verification: verification["signers"][0].update(
                {"identity": "different@example.org"}
            ),
            "$.verification.signers.operator.identity",
            id="operator",
        ),
        pytest.param(
            lambda verification: verification["target"].update({"identity_sha256": "b" * 64}),
            "$.verification.target.identity_sha256",
            id="target",
        ),
        pytest.param(
            lambda verification: verification.update({"trust_policy_sha256": "b" * 64}),
            "$.verification.trust_policy_sha256",
            id="trust-policy",
        ),
    ],
)
def test_verified_authorization_must_match_permit_and_scenario(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    expected_path: str,
) -> None:
    with pytest.raises(BundleValidationError) as caught:
        _mutated_verification_bundle(tmp_path, mutation)

    assert caught.value.json_path == expected_path


def test_runtime_target_identity_must_match_permit(tmp_path: Path) -> None:
    runtime = json.loads((FIXTURES / "hil-runtime.json").read_bytes())
    runtime["physical_targets"][1]["identity_sha256"] = "b" * 64
    runtime_path = tmp_path / "runtime.json"
    _write_json(runtime_path, runtime)

    with pytest.raises(BundleValidationError) as caught:
        _valid_bundle(runtime_path=runtime_path)

    assert caught.value.json_path.endswith(".identity_sha256")

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, NotRequired, TypedDict, cast

from robotics_runtime_contracts import (
    ProviderRequirementError,
    loads_mapping,
    schema_for_role,
    validate_document,
    validate_provider_requirements,
    validate_role,
)

from robotics_acceptance_harness.authorization import (
    AuthorizationIssue,
    evaluate_physical_authorization,
)


class RuntimeExecutionDocument(TypedDict):
    target_environment: str
    data_source: str
    plant_backend: str
    time_mode: str
    data_plane_profile: str


class ScenarioExecutionDocument(RuntimeExecutionDocument):
    security_profile: str


class ScenarioDocument(TypedDict):
    schema_version: str
    scenario_id: str
    execution: ScenarioExecutionDocument
    expected_ros_graph: Mapping[str, tuple[Mapping[str, Any], ...]]
    forbidden_ros_graph: Mapping[str, tuple[Any, ...]]
    timeouts: Mapping[str, float]
    time_policy: Mapping[str, Any]
    data_plane_policy: Mapping[str, Any]
    evidence_policy: Mapping[str, Any]
    assertions: tuple[Mapping[str, Any], ...]
    evaluator_requirements: tuple[Mapping[str, Any], ...]
    provider_requirements: Mapping[str, Any]
    model_manifest_sha256: NotRequired[str]
    dataset_manifest_sha256: NotRequired[str]


class RuntimeWorkloadDocument(TypedDict):
    kind: str
    model: NotRequired[Mapping[str, Any]]
    inference: NotRequired[Mapping[str, Any]]


class RuntimeDocument(TypedDict):
    schema_version: str
    execution: RuntimeExecutionDocument
    security: Mapping[str, Any]
    workload: RuntimeWorkloadDocument
    execution_subject: Mapping[str, Any]
    ros: Mapping[str, Any]
    evaluator_bindings: tuple[Mapping[str, Any], ...]
    provider_bindings: tuple[Mapping[str, Any], ...]
    data_plane: NotRequired[Mapping[str, Any]]


class BundleValidationError(ValueError):
    """Raised when individually valid execution documents contradict each other."""

    def __init__(
        self,
        json_path: str,
        message: str,
        *,
        related: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.json_path = json_path
        self.validation_message = message
        self.issues = ((json_path, message), *related)
        super().__init__("; ".join(f"{path}: {detail}" for path, detail in self.issues))

    @classmethod
    def from_authorization_issues(
        cls,
        issues: tuple[AuthorizationIssue, ...],
    ) -> BundleValidationError:
        first, *related = issues
        return cls(
            first.json_path,
            first.message,
            related=tuple((issue.json_path, issue.message) for issue in related),
        )


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    path: Path
    data: Mapping[str, Any]
    sha256: str

    @property
    def schema_version(self) -> str:
        return str(self.data["schema_version"])


@dataclass(frozen=True, slots=True)
class DocumentBundle:
    scenario: LoadedDocument
    runtime: LoadedDocument
    model: LoadedDocument | None = None
    dataset: LoadedDocument | None = None
    permit: LoadedDocument | None = None
    verification: LoadedDocument | None = None

    @property
    def scenario_data(self) -> ScenarioDocument:
        return cast(ScenarioDocument, self.scenario.data)

    @property
    def runtime_data(self) -> RuntimeDocument:
        return cast(RuntimeDocument, self.runtime.data)


def _read_mapping(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise BundleValidationError("$", f"cannot read {path}: {error}") from error
    try:
        value = loads_mapping(raw, source_name=str(path))
    except ValueError as error:
        raise BundleValidationError("$", f"cannot parse {path}: {error}") from error
    return raw, value


def load_document(
    path: str | Path,
    *,
    expected_role: str | None = None,
    extension_schemas: Mapping[str, bytes | str] | None = None,
) -> LoadedDocument:
    """Load, validate, hash, and freeze one contract document."""

    resolved_path = Path(path).expanduser().resolve()
    raw, value = _read_mapping(resolved_path)
    schema_version = value.get("schema_version")
    if expected_role is not None and schema_version != schema_for_role(expected_role):
        raise BundleValidationError(
            "$.schema_version",
            f"expected {schema_for_role(expected_role)}; received {schema_version!r}",
        )
    try:
        if expected_role is None:
            validate_document(value, extension_schemas=extension_schemas)
        else:
            validate_role(value, expected_role, extension_schemas=extension_schemas)
    except ValueError as error:
        raise BundleValidationError("$", f"invalid {resolved_path}: {error}") from error
    return LoadedDocument(
        path=resolved_path,
        data=_freeze(value),
        sha256=sha256(raw).hexdigest(),
    )


def _require_equal(path: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        raise BundleValidationError(path, f"expected {expected!r}; received {actual!r}")


def _validate_execution_alignment(
    scenario: ScenarioDocument,
    runtime: RuntimeDocument,
) -> None:
    scenario_execution = scenario["execution"]
    runtime_execution = runtime["execution"]
    for field in (
        "target_environment",
        "data_source",
        "plant_backend",
        "time_mode",
        "data_plane_profile",
    ):
        _require_equal(
            f"$.runtime.execution.{field}",
            scenario_execution[field],
            runtime_execution[field],
        )
    _require_equal(
        "$.runtime.security.profile",
        scenario_execution["security_profile"],
        runtime["security"]["profile"],
    )


def _validate_provider_alignment(
    scenario: ScenarioDocument,
    runtime: RuntimeDocument,
) -> None:
    try:
        validate_provider_requirements(
            scenario["provider_requirements"],
            runtime["provider_bindings"],
        )
    except ProviderRequirementError as error:
        raise BundleValidationError("$.runtime.provider_bindings", str(error)) from error


def _validate_model_alignment(
    scenario: ScenarioDocument,
    runtime: RuntimeDocument,
    model: LoadedDocument | None,
) -> None:
    declared_digest = scenario.get("model_manifest_sha256")
    workload = runtime["workload"]
    if declared_digest is None:
        if workload["kind"] == "inference":
            raise BundleValidationError(
                "$.scenario.model_manifest_sha256",
                "inference workload requires a declared model manifest",
            )
        if model is not None:
            raise BundleValidationError("$.model", "model document was not requested by scenario")
        return

    if model is None:
        raise BundleValidationError("$.model", "scenario requires a model manifest")
    _require_equal("$.model.sha256", declared_digest, model.sha256)
    if workload["kind"] != "inference":
        raise BundleValidationError(
            "$.runtime.workload.kind",
            "scenario declares a model but runtime reports no inference workload",
        )
    _require_equal(
        "$.runtime.workload.model.manifest_sha256",
        model.sha256,
        workload["model"]["manifest_sha256"],
    )
    _require_equal(
        "$.runtime.workload.model.artifact_sha256",
        model.data["target"]["sha256"],
        workload["model"]["artifact_sha256"],
    )
    _require_equal(
        "$.runtime.workload.model.format",
        model.data["target"]["format"],
        workload["model"]["format"],
    )
    _require_equal(
        "$.runtime.workload.inference.actual_provider",
        model.data["target"]["execution_provider"],
        workload["inference"]["actual_provider"],
    )


def _validate_dataset_alignment(
    scenario: ScenarioDocument,
    dataset: LoadedDocument | None,
) -> None:
    declared_digest = scenario.get("dataset_manifest_sha256")
    if declared_digest is None:
        if dataset is not None:
            raise BundleValidationError("$.dataset", "dataset was not requested by scenario")
        return
    if dataset is None:
        raise BundleValidationError("$.dataset", "scenario requires a dataset manifest")
    _require_equal("$.dataset.sha256", declared_digest, dataset.sha256)


def load_bundle(
    scenario_path: str | Path,
    *,
    runtime_path: str | Path | None = None,
    model_path: str | Path | None = None,
    dataset_path: str | Path | None = None,
    permit_path: str | Path | None = None,
    verification_path: str | Path | None = None,
    extension_schemas: Mapping[str, bytes | str] | None = None,
    now: datetime | None = None,
) -> DocumentBundle:
    """Load and cross-check all documents required by one acceptance execution."""

    scenario = load_document(
        scenario_path,
        expected_role="acceptance_scenario",
        extension_schemas=extension_schemas,
    )
    if runtime_path is None:
        raise BundleValidationError(
            "$.runtime",
            f"{scenario.schema_version} requires a runtime manifest",
        )
    runtime = load_document(
        runtime_path,
        expected_role="runtime_manifest",
    )
    model = (
        load_document(model_path, expected_role="model_artifact_manifest")
        if model_path is not None
        else None
    )
    dataset = (
        load_document(dataset_path, expected_role="dataset_manifest")
        if dataset_path is not None
        else None
    )
    permit = (
        load_document(permit_path, expected_role="execution_permit")
        if permit_path is not None
        else None
    )
    verification = (
        load_document(
            verification_path,
            expected_role="execution_verification",
        )
        if verification_path is not None
        else None
    )

    scenario_data = cast(ScenarioDocument, scenario.data)
    runtime_data = cast(RuntimeDocument, runtime.data)
    _validate_execution_alignment(scenario_data, runtime_data)
    _validate_provider_alignment(scenario_data, runtime_data)
    _require_equal(
        "$.runtime.evaluator_bindings",
        scenario_data["evaluator_requirements"],
        runtime_data["evaluator_bindings"],
    )
    _validate_model_alignment(scenario_data, runtime_data, model)
    _validate_dataset_alignment(scenario_data, dataset)
    checked_at = now or datetime.now(UTC)
    issues = evaluate_physical_authorization(
        scenario=scenario_data,
        scenario_sha256=scenario.sha256,
        runtime=runtime_data,
        permit=permit.data if permit is not None else None,
        permit_sha256=permit.sha256 if permit is not None else None,
        permit_path=permit.path if permit is not None else None,
        verification=verification.data if verification is not None else None,
        verification_sha256=verification.sha256 if verification is not None else None,
        verification_path=verification.path if verification is not None else None,
        now=checked_at,
    )
    if issues:
        raise BundleValidationError.from_authorization_issues(issues)
    return DocumentBundle(
        scenario=scenario,
        runtime=runtime,
        model=model,
        dataset=dataset,
        permit=permit,
        verification=verification,
    )

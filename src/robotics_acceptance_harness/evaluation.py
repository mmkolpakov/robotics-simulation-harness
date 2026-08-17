from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.machinery import PathFinder
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path, PurePosixPath
from typing import Any, cast

from packaging.utils import canonicalize_name

from robotics_acceptance_harness.documents import DocumentBundle
from robotics_acceptance_harness.evidence import VerifiedEvidence
from robotics_acceptance_harness.metrics import (
    AssertionEvaluation,
    MetricPoint,
    evaluate_metric_assertions,
    validate_metric_definitions,
)
from robotics_acceptance_harness.policy import (
    evaluate_data_plane_policy,
    evaluate_evidence_policy,
)
from robotics_acceptance_harness.receipts import VerifiedReceiptSet

EVALUATOR_ENTRY_POINT_GROUP = "robotics_acceptance.evaluators"
_INSTALLER_GENERATED_NAMES = frozenset({"INSTALLER", "RECORD", "REQUESTED", "direct_url.json"})


class EvaluationError(ValueError):
    """Raised when an evaluator violates the public extension contract."""

    error_id = "evaluation.invalid"


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Immutable inputs shared by live and offline acceptance evaluation."""

    run_id: str
    domain_id: str
    bundle: DocumentBundle
    evidence: VerifiedEvidence
    metric_samples: tuple[MetricPoint, ...]
    window_start_ns: int
    window_end_ns: int

    @property
    def scenario(self) -> Mapping[str, Any]:
        return self.bundle.scenario.data

    @property
    def runtime(self) -> Mapping[str, Any]:
        return self.bundle.runtime.data

    @property
    def evidence_sha256(self) -> frozenset[str]:
        return frozenset(str(item["sha256"]) for item in self.evidence.links)


type ProductEvaluator = Callable[[EvaluationContext], Iterable[AssertionEvaluation]]


def _load_evaluator(entry_point: EntryPoint) -> ProductEvaluator:
    try:
        evaluator = entry_point.load()
    except Exception as error:
        raise EvaluationError(
            f"cannot load evaluator entry point {entry_point.name!r}: {error}"
        ) from error
    if not callable(evaluator):
        raise EvaluationError(f"entry point {entry_point.name!r} is not callable")
    return cast(ProductEvaluator, evaluator)


def _verify_installed_record(entry_point: EntryPoint) -> frozenset[Path]:
    distribution = entry_point.dist
    if distribution is None or distribution.files is None:
        raise EvaluationError(f"entry point {entry_point.name!r} has no installed file manifest")
    record_files = [path for path in distribution.files if str(path).endswith(".dist-info/RECORD")]
    if len(record_files) != 1:
        raise EvaluationError(f"distribution {distribution.name!r} must contain exactly one RECORD")
    record_name = PurePosixPath(str(record_files[0]).replace("\\", "/"))
    dist_info = record_name.parent
    hashed_paths = {
        Path(path.locate()).resolve() for path in distribution.files if path.hash is not None
    }

    verified_files = 0
    for package_path in distribution.files:
        installed_name = PurePosixPath(str(package_path).replace("\\", "/"))
        installed_path = Path(package_path.locate())
        if not installed_path.is_file():
            raise EvaluationError(f"installed evaluator file is missing: {package_path}")
        file_hash = package_path.hash
        if file_hash is None:
            installer_generated = (
                installed_name.parent == dist_info
                and installed_name.name in _INSTALLER_GENERATED_NAMES
            )
            if not installer_generated:
                raise EvaluationError(
                    f"installed evaluator file has no RECORD hash: {package_path}"
                )
            continue
        try:
            observed = (
                base64.urlsafe_b64encode(
                    hashlib.new(file_hash.mode, installed_path.read_bytes()).digest()
                )
                .rstrip(b"=")
                .decode("ascii")
            )
        except ValueError as error:
            raise EvaluationError(f"unsupported RECORD hash: {file_hash.mode}") from error
        if observed != file_hash.value:
            raise EvaluationError(f"installed evaluator file differs from RECORD: {package_path}")
        verified_files += 1
        if installed_name.suffix == ".py":
            bytecode_candidates = [installed_path.with_suffix(".pyc")]
            pycache = installed_path.parent / "__pycache__"
            if pycache.is_dir():
                bytecode_candidates.extend(pycache.glob(f"{installed_path.stem}.*.pyc"))
            unverified = [
                path
                for path in bytecode_candidates
                if path.is_file() and path.resolve() not in hashed_paths
            ]
            if unverified:
                raise EvaluationError(
                    f"installed evaluator has unverified bytecode cache: {unverified[0]}"
                )
    if verified_files == 0:
        raise EvaluationError(f"distribution {distribution.name!r} RECORD has no file digests")
    return frozenset(hashed_paths)


def _verify_entry_point_origin(entry_point: EntryPoint, hashed_paths: frozenset[Path]) -> None:
    search_path: Sequence[str] | None = None
    qualified_name = ""
    spec = None
    for part in entry_point.module.split("."):
        qualified_name = f"{qualified_name}.{part}" if qualified_name else part
        spec = PathFinder.find_spec(qualified_name, search_path)
        if spec is None:
            raise EvaluationError(f"cannot resolve evaluator module {entry_point.module!r}")
        search_path = spec.submodule_search_locations
    if spec is None or spec.origin in {None, "built-in", "frozen"}:
        raise EvaluationError(f"evaluator module {entry_point.module!r} has no file origin")
    origin = Path(spec.origin).resolve()
    if origin not in hashed_paths:
        raise EvaluationError(
            f"evaluator module {entry_point.module!r} resolves outside its verified RECORD"
        )


def _qualified_entry_points(
    requirements: Sequence[Mapping[str, Any]],
    receipts: VerifiedReceiptSet,
) -> tuple[EntryPoint, ...]:
    installed = tuple(entry_points(group=EVALUATOR_ENTRY_POINT_GROUP))
    qualified: list[EntryPoint] = []
    for requirement in requirements:
        namespace = str(requirement["namespace"])
        candidates = [entry_point for entry_point in installed if entry_point.name == namespace]
        if len(candidates) != 1:
            raise EvaluationError(
                f"expected one evaluator entry point for {namespace!r}; found {len(candidates)}"
            )
        entry_point = candidates[0]
        distribution = entry_point.dist
        if distribution is None:
            raise EvaluationError(f"evaluator {namespace!r} has no owning distribution")
        observed = (
            entry_point.value,
            canonicalize_name(distribution.name),
            distribution.version,
        )
        expected = (
            requirement["entry_point"],
            canonicalize_name(str(requirement["distribution"])),
            requirement["version"],
        )
        if observed != expected:
            raise EvaluationError(f"installed evaluator {namespace!r} differs from its binding")
        receipts.verify_artifact(
            str(requirement["receipt_sha256"]),
            {"sha256": requirement["artifact_sha256"]},
        )
        hashed_paths = _verify_installed_record(entry_point)
        _verify_entry_point_origin(entry_point, hashed_paths)
        qualified.append(entry_point)
    if {str(item["receipt_sha256"]) for item in requirements} != set(receipts.by_digest):
        raise EvaluationError("evaluator qualification contains unreferenced receipts")
    return tuple(qualified)


def _installed_evaluators(
    requirements: Sequence[Mapping[str, Any]],
    receipts: VerifiedReceiptSet,
) -> tuple[tuple[str, ProductEvaluator], ...]:
    return tuple(
        (entry_point.name, _load_evaluator(entry_point))
        for entry_point in _qualified_entry_points(requirements, receipts)
    )


def _product_evaluations(
    context: EvaluationContext,
    evaluators: Sequence[tuple[str, ProductEvaluator]],
) -> tuple[AssertionEvaluation, ...]:
    evaluations: list[AssertionEvaluation] = []
    for namespace, evaluator in evaluators:
        if namespace.count(".") < 1:
            raise EvaluationError(
                f"evaluator namespace {namespace!r} must be a reverse-domain name"
            )
        try:
            produced = evaluator(context)
            for evaluation in produced:
                if not isinstance(evaluation, AssertionEvaluation):
                    raise EvaluationError(
                        f"evaluator {namespace!r} returned {type(evaluation).__name__}; "
                        "expected AssertionEvaluation"
                    )
                if evaluation.source != "product" or evaluation.namespace != namespace:
                    raise EvaluationError(
                        f"evaluator {namespace!r} must mark every result as its product namespace"
                    )
                if not evaluation.assertion_id.startswith(f"{namespace}."):
                    raise EvaluationError(
                        f"assertion {evaluation.assertion_id!r} is outside namespace {namespace!r}"
                    )
                if not evaluation.evidence_sha256:
                    raise EvaluationError(
                        f"product assertion {evaluation.assertion_id!r} has no evidence digest"
                    )
                missing = set(evaluation.evidence_sha256) - context.evidence_sha256
                if missing:
                    raise EvaluationError(
                        "product assertion "
                        f"{evaluation.assertion_id!r} references unknown evidence "
                        f"{sorted(missing)}"
                    )
                evaluations.append(evaluation)
        except EvaluationError:
            raise
        except Exception as error:
            raise EvaluationError(f"evaluator {namespace!r} failed: {error}") from error
    return tuple(evaluations)


def evaluate_acceptance(
    context: EvaluationContext,
    *,
    evaluators: Sequence[tuple[str, ProductEvaluator]] | None = None,
    evaluator_receipts: VerifiedReceiptSet | None = None,
) -> tuple[AssertionEvaluation, ...]:
    """Evaluate evidence through the canonical core and installed product evaluators."""

    scenario = context.scenario
    validate_metric_definitions(scenario["metric_definitions"], context.metric_samples)
    evaluations = list(
        evaluate_metric_assertions(
            scenario["assertions"],
            context.metric_samples,
            window_start_ns=context.window_start_ns,
            window_end_ns=context.window_end_ns,
        )
    )
    evaluations.extend(
        evaluate_data_plane_policy(
            scenario["data_plane_policy"],
            context.runtime,
            context.metric_samples,
            domain_id=context.domain_id,
            window_start_ns=context.window_start_ns,
            window_end_ns=context.window_end_ns,
        )
    )
    evaluations.extend(evaluate_evidence_policy(scenario["evidence_policy"], context.evidence))
    evaluations.extend(
        _product_evaluations(
            context,
            (
                tuple(evaluators)
                if evaluators is not None
                else _installed_evaluators(
                    scenario["evaluator_requirements"],
                    evaluator_receipts or VerifiedReceiptSet({}),
                )
            ),
        )
    )
    identifiers = [item.assertion_id for item in evaluations]
    if len(identifiers) != len(set(identifiers)):
        duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
        raise EvaluationError(f"duplicate assertion identifiers: {duplicates}")
    return tuple(evaluations)


def evaluator_inventory(
    requirements: Sequence[Mapping[str, Any]] = (),
    receipts: VerifiedReceiptSet | None = None,
) -> tuple[Mapping[str, str], ...]:
    """Describe installed product evaluators without importing their targets."""

    installed = (
        _qualified_entry_points(requirements, receipts or VerifiedReceiptSet({}))
        if requirements
        else tuple(entry_points(group=EVALUATOR_ENTRY_POINT_GROUP))
    )
    inventory: list[Mapping[str, str]] = []
    for entry_point in sorted(
        installed,
        key=lambda item: (item.name, item.value),
    ):
        assert isinstance(entry_point, EntryPoint)
        inventory.append(
            {
                "namespace": entry_point.name,
                "target": entry_point.value,
                "distribution": (
                    entry_point.dist.name if entry_point.dist is not None else "unknown"
                ),
                "version": (
                    entry_point.dist.version if entry_point.dist is not None else "unknown"
                ),
                "status": "qualified" if requirements else "discovered",
            }
        )
    return tuple(inventory)


__all__ = [
    "EVALUATOR_ENTRY_POINT_GROUP",
    "AssertionEvaluation",
    "EvaluationContext",
    "EvaluationError",
    "ProductEvaluator",
    "evaluate_acceptance",
    "evaluator_inventory",
]

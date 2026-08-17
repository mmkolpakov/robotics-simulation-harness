from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import yaml

from robotics_acceptance_harness.documents import DocumentBundle, LoadedDocument, load_bundle
from robotics_acceptance_harness.evaluation import (
    EvaluationContext,
    EvaluationError,
    _verify_entry_point_origin,
    _verify_installed_record,
    evaluate_acceptance,
)
from robotics_acceptance_harness.evidence import VerifiedEvidence
from robotics_acceptance_harness.metrics import (
    AssertionEvaluation,
    MetricDefinitionError,
    MetricSample,
    evaluate_metric_assertions,
    validate_metric_definitions,
)
from robotics_acceptance_harness.receipts import load_verified_receipts
from tests.support import write_verified_receipt

FIXTURES = Path(__file__).parent / "fixtures" / "simulation"
EVIDENCE_DIGEST = "a" * 64


class FakePackagePath:
    def __init__(self, name: str, path: Path, digest: str | None) -> None:
        self._name = name
        self._path = path
        self.hash = SimpleNamespace(mode="sha256", value=digest) if digest is not None else None

    @property
    def name(self) -> str:
        return Path(self._name).name

    @property
    def suffix(self) -> str:
        return Path(self._name).suffix

    def locate(self) -> Path:
        return self._path

    def __str__(self) -> str:
        return self._name


def _record_digest(path: Path) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=").decode()
    )


def _installed_distribution(tmp_path: Path) -> tuple[SimpleNamespace, dict[str, FakePackagePath]]:
    files: dict[str, FakePackagePath] = {}
    for name, content in (
        ("example_evaluator.py", "def evaluate():\n    return ()\n"),
        ("example_evaluator-1.0.dist-info/METADATA", "Name: example-evaluator\n"),
        (
            "example_evaluator-1.0.dist-info/entry_points.txt",
            "[robotics_acceptance.evaluators]\norg.example = example_evaluator:evaluate\n",
        ),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        files[name] = FakePackagePath(name, path, _record_digest(path))
    record_name = "example_evaluator-1.0.dist-info/RECORD"
    record_path = tmp_path / record_name
    record_path.write_text("fixture RECORD\n", encoding="utf-8")
    files[record_name] = FakePackagePath(record_name, record_path, None)
    distribution = SimpleNamespace(name="example-evaluator", files=tuple(files.values()))
    return SimpleNamespace(name="org.example", dist=distribution), files


def context(bundle: DocumentBundle | None = None) -> EvaluationContext:
    bundle = bundle or load_bundle(
        FIXTURES / "scenario.yaml",
        runtime_path=FIXTURES / "runtime.yaml",
    )
    evidence = VerifiedEvidence(
        LoadedDocument(
            Path("evidence.json"),
            MappingProxyType(
                {
                    "finalized": True,
                    "policy_observation": {
                        "recording_mode": "on_failure",
                        "compression": "zstd",
                        "upload_mode": "local_only",
                        "remote_sink_used": False,
                        "spool_peak_size_bytes": 1,
                        "upload_lag_max_sec": 0,
                    },
                    "artifacts": [
                        {
                            "artifact_id": "fixture-observation",
                            "kind": "observation",
                            "storage_state": "local",
                            "sha256": EVIDENCE_DIGEST,
                            "size_bytes": 1,
                            "retention_class": "pull-request-7d",
                        }
                    ],
                }
            ),
            "b" * 64,
        ),
        (MappingProxyType({"sha256": EVIDENCE_DIGEST}),),
        MappingProxyType({}),
    )
    return EvaluationContext("run-test", "primary", bundle, evidence, (), 0, 1)


def test_evaluator_module_requires_an_installed_record_hash(tmp_path: Path) -> None:
    entry_point, files = _installed_distribution(tmp_path)
    files["example_evaluator.py"].hash = None

    with pytest.raises(EvaluationError, match="has no RECORD hash"):
        _verify_installed_record(entry_point)


def test_sourceless_bytecode_cannot_bypass_the_record_hash(tmp_path: Path) -> None:
    entry_point, files = _installed_distribution(tmp_path)
    pyc_name = "__pycache__/example_evaluator.cpython-312.pyc"
    pyc_path = tmp_path / pyc_name
    pyc_path.parent.mkdir(parents=True, exist_ok=True)
    pyc_path.write_bytes(b"untrusted bytecode")
    pyc = FakePackagePath(pyc_name, pyc_path, None)
    entry_point.dist.files = tuple(
        item for name, item in files.items() if name != "example_evaluator.py"
    ) + (pyc,)

    with pytest.raises(EvaluationError, match="has no RECORD hash"):
        _verify_installed_record(entry_point)


def test_unregistered_generated_bytecode_is_rejected(tmp_path: Path) -> None:
    entry_point, _files = _installed_distribution(tmp_path)
    pyc_name = "__pycache__/example_evaluator.cpython-312.pyc"
    pyc_path = tmp_path / pyc_name
    pyc_path.parent.mkdir(parents=True, exist_ok=True)
    pyc_path.write_bytes(b"derived bytecode")

    with pytest.raises(EvaluationError, match="unverified bytecode cache"):
        _verify_installed_record(entry_point)


def test_installer_metadata_exception_is_scoped_to_dist_info(tmp_path: Path) -> None:
    entry_point, files = _installed_distribution(tmp_path)
    installer_path = tmp_path / "INSTALLER"
    installer_path.write_text("untrusted\n", encoding="utf-8")
    entry_point.dist.files = (
        *files.values(),
        FakePackagePath("INSTALLER", installer_path, None),
    )

    with pytest.raises(EvaluationError, match="has no RECORD hash"):
        _verify_installed_record(entry_point)


def test_entry_point_cannot_be_shadowed_outside_its_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted" / "example_evaluator.py"
    trusted.parent.mkdir()
    trusted.write_text("trusted = True\n", encoding="utf-8")
    shadow = tmp_path / "shadow" / "example_evaluator.py"
    shadow.parent.mkdir()
    shadow.write_text("trusted = False\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(shadow.parent))
    entry_point = SimpleNamespace(name="org.example", module="example_evaluator")

    with pytest.raises(EvaluationError, match="outside its verified RECORD"):
        _verify_entry_point_origin(entry_point, frozenset({trusted.resolve()}))


@pytest.mark.parametrize(
    "name",
    [
        "example_evaluator.py",
        "example_evaluator-1.0.dist-info/entry_points.txt",
    ],
)
def test_evaluator_import_files_must_match_the_installed_record(
    tmp_path: Path,
    name: str,
) -> None:
    entry_point, files = _installed_distribution(tmp_path)
    files[name]._path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(EvaluationError, match="differs from RECORD"):
        _verify_installed_record(entry_point)


def test_product_evaluator_is_namespaced_and_evidence_bound() -> None:
    def evaluator(_context: EvaluationContext) -> tuple[AssertionEvaluation, ...]:
        return (
            AssertionEvaluation(
                "org.example.sorting.detected",
                "passed",
                1,
                "1",
                source="product",
                namespace="org.example.sorting",
                evidence_sha256=(EVIDENCE_DIGEST,),
            ),
        )

    evaluations = evaluate_acceptance(
        context(),
        evaluators=(("org.example.sorting", evaluator),),
    )

    assert evaluations[-1].assertion_id == "org.example.sorting.detected"
    assert evaluations[-1].source == "product"


def test_product_evaluator_cannot_reference_unverified_evidence() -> None:
    def evaluator(_context: EvaluationContext) -> tuple[AssertionEvaluation, ...]:
        return (
            AssertionEvaluation(
                "org.example.sorting.detected",
                "passed",
                1,
                "1",
                source="product",
                namespace="org.example.sorting",
                evidence_sha256=("f" * 64,),
            ),
        )

    with pytest.raises(EvaluationError, match="unknown evidence"):
        evaluate_acceptance(context(), evaluators=(("org.example.sorting", evaluator),))


def test_product_evaluator_exception_is_a_stable_evaluation_error() -> None:
    def evaluator(_context: EvaluationContext) -> tuple[AssertionEvaluation, ...]:
        raise KeyError("missing calibration")

    with pytest.raises(EvaluationError, match="evaluator 'org.example.sorting' failed"):
        evaluate_acceptance(context(), evaluators=(("org.example.sorting", evaluator),))


def test_installed_distribution_contributes_a_product_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "example_evaluator.py"
    module_path.write_text(
        """\
from robotics_acceptance_harness import AssertionEvaluation

def evaluate(context):
    return (AssertionEvaluation(
        'org.example.sorting.detected', 'passed', 1, '1',
        source='product', namespace='org.example.sorting',
        evidence_sha256=(next(iter(context.evidence_sha256)),),
    ),)
""",
        encoding="utf-8",
    )
    metadata = tmp_path / "example_evaluator-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: example-evaluator\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (metadata / "entry_points.txt").write_text(
        "[robotics_acceptance.evaluators]\norg.example.sorting = example_evaluator:evaluate\n",
        encoding="utf-8",
    )
    record_path = metadata / "RECORD"
    record_lines = []
    for path in (module_path, metadata / "METADATA", metadata / "entry_points.txt"):
        payload = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        record_lines.append(
            f"{path.relative_to(tmp_path).as_posix()},sha256={digest},{len(payload)}"
        )
    record_lines.append(f"{record_path.relative_to(tmp_path).as_posix()},,")
    record_path.write_text("\n".join(record_lines) + "\n", encoding="utf-8")
    artifact_sha256 = "d" * 64
    artifact = {
        "uri": "file:///qualified/example_evaluator-1.0-py3-none-any.whl",
        "sha256": artifact_sha256,
        "size_bytes": 1024,
        "media_type": "application/vnd.python.wheel",
        "immutable_revision": f"sha256:{artifact_sha256}",
    }
    chain = write_verified_receipt(
        tmp_path,
        artifact,
        stem="evaluator",
    )
    requirement = {
        "namespace": "org.example.sorting",
        "entry_point": "example_evaluator:evaluate",
        "distribution": "example-evaluator",
        "version": "1.0",
        "artifact_sha256": artifact_sha256,
        "receipt_sha256": chain["receipt_sha256"],
    }
    scenario = yaml.safe_load((FIXTURES / "scenario.yaml").read_text(encoding="utf-8"))
    scenario["evaluator_requirements"] = [requirement]
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    runtime = yaml.safe_load((FIXTURES / "runtime.yaml").read_text(encoding="utf-8"))
    runtime["evaluator_bindings"] = [requirement]
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    evaluations = evaluate_acceptance(
        context(load_bundle(scenario_path, runtime_path=runtime_path)),
        evaluator_receipts=load_verified_receipts(
            receipt_paths=[chain["receipt"]],
            verification_paths=[chain["verification"]],
            dependency_paths=chain["dependencies"],
        ),
    )

    assert evaluations[-1].assertion_id == "org.example.sorting.detected"


def test_duration_predicate_requires_contiguous_coverage() -> None:
    assertion = {
        "assertion_id": "temperature-stable",
        "kind": "metric_duration",
        "metric_name": "org.example.temperature",
        "unit": "C",
        "operator": "lte",
        "threshold": 10,
        "window_sec": 10,
        "max_sample_gap_sec": 5,
        "duration_requirement": {"kind": "minimum_contiguous", "duration_sec": 8},
        "attribute_match": {},
    }
    samples = (
        MetricSample("org.example.temperature", 8, "C", 0),
        MetricSample("org.example.temperature", 9, "C", 5_000_000_000),
        MetricSample("org.example.temperature", 9, "C", 10_000_000_000),
    )

    evaluation = evaluate_metric_assertions(
        (assertion,),
        samples,
        window_start_ns=0,
        window_end_ns=10_000_000_000,
    )[0]

    assert evaluation.status == "passed"
    assert evaluation.observed_value == 10


def test_duration_predicate_never_merges_distinct_attribute_series() -> None:
    assertion = {
        "assertion_id": "temperature-stable",
        "kind": "metric_duration",
        "metric_name": "org.example.temperature",
        "unit": "C",
        "operator": "lte",
        "threshold": 10,
        "window_sec": 10,
        "max_sample_gap_sec": 6,
        "duration_requirement": {"kind": "minimum_contiguous", "duration_sec": 8},
        "attribute_match": {},
    }
    samples = (
        MetricSample("org.example.temperature", 8, "C", 0, {"sensor": "a"}),
        MetricSample(
            "org.example.temperature",
            8,
            "C",
            5_000_000_000,
            {"sensor": "b"},
        ),
        MetricSample(
            "org.example.temperature",
            8,
            "C",
            10_000_000_000,
            {"sensor": "a"},
        ),
    )

    evaluation = evaluate_metric_assertions(
        (assertion,),
        samples,
        window_start_ns=0,
        window_end_ns=10_000_000_000,
    )[0]

    assert evaluation.status == "error"
    assert "exactly one attribute series" in evaluation.message


def test_missing_declared_metric_becomes_an_error_result() -> None:
    definitions = (
        {
            "metric_name": "org.example.temperature",
            "unit": "C",
            "instrument_kind": "gauge",
            "temporality": "instantaneous",
        },
    )
    assertion = {
        "assertion_id": "temperature",
        "kind": "metric",
        "metric_name": "org.example.temperature",
        "unit": "C",
        "operator": "lte",
        "threshold": 10,
        "aggregation": "max",
        "window_sec": 10,
        "attribute_match": {},
    }

    validate_metric_definitions(definitions, ())
    evaluation = evaluate_metric_assertions((assertion,), ())[0]

    assert evaluation.status == "error"
    assert "no samples" in evaluation.message


def test_metric_definition_rejects_instrument_drift() -> None:
    definitions = (
        {
            "metric_name": "org.example.temperature",
            "unit": "C",
            "instrument_kind": "gauge",
            "temporality": "instantaneous",
        },
    )
    samples = (
        MetricSample(
            "org.example.temperature",
            1,
            "C",
            1,
            instrument_kind="sum",
            temporality="delta",
        ),
    )

    with pytest.raises(MetricDefinitionError, match="expects gauge"):
        validate_metric_definitions(definitions, samples)

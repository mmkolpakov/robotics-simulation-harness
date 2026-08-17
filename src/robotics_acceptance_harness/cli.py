from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from robotics_acceptance_harness import __version__
from robotics_acceptance_harness.aggregate import (
    aggregate_results,
    evaluate_transport_qualification,
)
from robotics_acceptance_harness.application import (
    evaluate_from_evidence,
    explain_bundle,
    run_verification,
)
from robotics_acceptance_harness.campaign import aggregate_campaign
from robotics_acceptance_harness.diagnostics import (
    doctor_report,
    report_markdown,
    why_report,
    write_error_diagnostic,
)
from robotics_acceptance_harness.documents import DocumentBundle, load_bundle, load_document
from robotics_acceptance_harness.evidence import load_evidence_index
from robotics_acceptance_harness.extension_schemas import load_extension_schemas
from robotics_acceptance_harness.hardware_timing import evaluate_hardware_timing
from robotics_acceptance_harness.metrics import MetricSample
from robotics_acceptance_harness.otel import (
    OTLP_JSON_LINES_MEDIA_TYPE,
    load_otlp_json_metrics,
    select_metric_points,
)
from robotics_acceptance_harness.receipts import VerifiedReceiptSet, load_verified_receipts
from robotics_acceptance_harness.run_context import create_run_context, load_run_context


def _add_extension_schema_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--extension-schema",
        action="append",
        default=[],
        metavar="URI=PATH",
        help="Digest-pinned local extension schema; may be repeated.",
    )


def _add_bundle_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scenario", required=True, metavar="PATH")
    parser.add_argument("--runtime", required=True, metavar="PATH")
    parser.add_argument("--model", metavar="PATH")
    parser.add_argument("--dataset", metavar="PATH")
    parser.add_argument("--permit", metavar="PATH")
    parser.add_argument("--verification", metavar="PATH")
    parser.add_argument("--evaluator-receipt", action="append", default=[], metavar="PATH")
    parser.add_argument("--evaluator-verification", action="append", default=[], metavar="PATH")
    parser.add_argument(
        "--evaluator-receipt-dependency",
        action="append",
        default=[],
        metavar="PATH",
    )
    _add_extension_schema_argument(parser)


def _add_evidence_receipt_arguments(
    parser: argparse.ArgumentParser,
    *,
    grouped: bool = False,
) -> None:
    metavar = "DOMAIN=PATH" if grouped else "PATH"
    parser.add_argument("--artifact-receipt", action="append", default=[], metavar=metavar)
    parser.add_argument("--artifact-verification", action="append", default=[], metavar=metavar)
    parser.add_argument("--receipt-dependency", action="append", default=[], metavar=metavar)


def _add_trace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--causal-chain",
        action="append",
        required=True,
        metavar="PATH",
        help="Causal-chain contract; may be repeated for branching flows.",
    )
    parser.add_argument(
        "--trace",
        action="append",
        required=True,
        metavar="DOMAIN=PATH",
        help="Verified per-domain OTLP trace evidence; may be repeated.",
    )
    parser.add_argument(
        "--evidence-index",
        action="append",
        required=True,
        metavar="DOMAIN=PATH",
        help="Finalized per-domain evidence index; may be repeated.",
    )
    parser.add_argument(
        "--channel-contract",
        action="append",
        required=True,
        metavar="PATH",
        help="Transport channel contract in causal order; may be repeated.",
    )
    _add_evidence_receipt_arguments(parser, grouped=True)
    parser.add_argument("--observation-output", required=True, metavar="DIR")
    parser.add_argument("--output", required=True, metavar="PATH")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robotics-acceptance",
        description="Attach-only acceptance observer for an existing ROS 2 execution.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_run = subparsers.add_parser(
        "create-run",
        help="Create a validated acceptance-run.v1 context.",
    )
    create_run.add_argument("--scenario", required=True, metavar="PATH")
    create_run.add_argument("--output", required=True, metavar="PATH")
    create_run.add_argument("--domain", action="append", required=True, metavar="ID=ROLE")
    create_run.add_argument("--time-authority", required=True, metavar="KIND")
    create_run.add_argument("--time-source", required=True, metavar="ID")
    create_run.add_argument("--run-id", metavar="RUN_ID")
    _add_extension_schema_argument(create_run)

    explain = subparsers.add_parser("explain", help="Validate and explain an execution bundle.")
    _add_bundle_arguments(explain)

    verify = subparsers.add_parser("verify", help="Observe and evaluate a running execution.")
    _add_bundle_arguments(verify)
    verify.add_argument("--run-id", required=True, metavar="RUN_ID")
    verify.add_argument(
        "--domain-id",
        required=True,
        metavar="DOMAIN_ID",
        help="Domain identifier declared by the acceptance run.",
    )
    verify.add_argument(
        "--run-context",
        required=True,
        metavar="PATH",
        help="Validated acceptance-run.v1 context.",
    )
    verify.add_argument("--evidence-index", required=True, metavar="PATH")
    _add_evidence_receipt_arguments(verify)
    verify.add_argument(
        "--otel-metrics",
        required=True,
        metavar="PATH",
        help="Newline-delimited OTLP JSON from the OpenTelemetry Collector file exporter.",
    )
    verify.add_argument(
        "--measurement-complete",
        required=True,
        metavar="PATH",
        help="Atomically create a marker after the measurement window closes.",
    )
    verify.add_argument("--output", required=True, metavar="DIR")
    verify.add_argument("--diagnostic-output", metavar="PATH")

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Evaluate finalized playback evidence without attaching to ROS.",
    )
    _add_bundle_arguments(evaluate)
    evaluate.add_argument("--run-id", required=True, metavar="RUN_ID")
    evaluate.add_argument("--domain-id", required=True, metavar="DOMAIN_ID")
    evaluate.add_argument("--run-context", required=True, metavar="PATH")
    evaluate.add_argument("--evidence-index", required=True, metavar="PATH")
    _add_evidence_receipt_arguments(evaluate)
    evaluate.add_argument("--otel-metrics", required=True, metavar="PATH")
    evaluate.add_argument("--window-start-ns", required=True, type=int)
    evaluate.add_argument("--window-end-ns", required=True, type=int)
    evaluate.add_argument("--output", required=True, metavar="DIR")
    evaluate.add_argument("--diagnostic-output", metavar="PATH")

    aggregate = subparsers.add_parser(
        "aggregate",
        help="Aggregate complete per-domain results for one run.",
    )
    aggregate.add_argument("--scenario", required=True, metavar="PATH")
    aggregate.add_argument("--run-context", required=True, metavar="PATH")
    aggregate.add_argument("--result", required=True, action="append", metavar="PATH")
    aggregate.add_argument(
        "--transport-qualification",
        metavar="PATH",
        help="Optional transport qualification for the same run.",
    )
    aggregate.add_argument("--output", required=True, metavar="PATH")
    _add_extension_schema_argument(aggregate)

    transport_evaluate = subparsers.add_parser(
        "transport-evaluate",
        help="Evaluate channel delivery and causal traces without a domain execution.",
    )
    transport_evaluate.add_argument("--run-id", required=True, metavar="RUN_ID")
    transport_evaluate.add_argument("--scenario", required=True, metavar="PATH")
    _add_extension_schema_argument(transport_evaluate)
    _add_trace_arguments(transport_evaluate)
    transport_evaluate.add_argument(
        "--clock-relation",
        action="append",
        default=[],
        metavar="PATH",
        help="Measured clock-relation contract; repeat for each directed domain pair.",
    )

    campaign = subparsers.add_parser(
        "campaign",
        help="Aggregate existing run verdicts into a campaign summary.",
    )
    campaign.add_argument("--scenario", required=True, metavar="PATH")
    campaign.add_argument("--run-context", action="append", required=True, metavar="PATH")
    campaign.add_argument("--aggregate", action="append", required=True, metavar="PATH")
    campaign.add_argument("--minimum-passed-runs", required=True, type=int)
    campaign.add_argument("--maximum-failed-runs", type=int, default=0)
    campaign.add_argument("--maximum-incomplete-runs", type=int, default=0)
    campaign.add_argument("--maximum-error-runs", type=int, default=0)
    campaign.add_argument("--campaign-id")
    campaign.add_argument("--output", required=True, metavar="PATH")
    _add_extension_schema_argument(campaign)

    doctor = subparsers.add_parser("doctor", help="Report runtime and evaluator readiness.")
    doctor.add_argument("--format", choices=("json", "markdown"), default="json")
    doctor.add_argument("--mode", choices=("live", "offline"), default="offline")
    doctor.add_argument("--evidence-dir", metavar="PATH")
    doctor.add_argument("--measurement-complete", metavar="PATH")
    doctor.add_argument("--scenario", metavar="PATH")
    doctor.add_argument("--evaluator-receipt", action="append", default=[], metavar="PATH")
    doctor.add_argument("--evaluator-verification", action="append", default=[], metavar="PATH")
    doctor.add_argument(
        "--evaluator-receipt-dependency",
        action="append",
        default=[],
        metavar="PATH",
    )

    why = subparsers.add_parser("why", help="Explain an acceptance result verdict.")
    why.add_argument("result", metavar="PATH")
    why.add_argument("--format", choices=("json", "markdown"), default="json")

    timing = subparsers.add_parser(
        "timing-check",
        help="Evaluate verified hardware-clock evidence against its scenario policy.",
    )
    timing.add_argument("--scenario", required=True, metavar="PATH")
    timing.add_argument("--run-context", required=True, metavar="PATH")
    timing.add_argument("--run-id", required=True, metavar="RUN_ID")
    timing.add_argument("--domain-id", required=True, metavar="DOMAIN_ID")
    timing.add_argument("--evidence-index", required=True, metavar="PATH")
    timing.add_argument("--otel-metrics", required=True, metavar="PATH")
    _add_evidence_receipt_arguments(timing)
    timing.add_argument(
        "--expect",
        choices=("within-policy", "out-of-policy"),
        default="within-policy",
    )
    _add_extension_schema_argument(timing)

    otel_summary = subparsers.add_parser(
        "otel-summary",
        help="Summarize normalized OTLP metric points without evaluating them.",
    )
    otel_summary.add_argument("--otel-metrics", required=True, metavar="PATH")

    return parser


def _keyed_values(values: Sequence[str], option: str) -> Mapping[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise ValueError(f"invalid {option} value: {value!r}")
        if key in parsed:
            raise ValueError(f"duplicate {option} key: {key}")
        parsed[key] = item
    return parsed


def _grouped_values(values: Sequence[str], option: str) -> Mapping[str, tuple[str, ...]]:
    parsed: dict[str, list[str]] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise ValueError(f"invalid {option} value: {value!r}")
        parsed.setdefault(key, []).append(item)
    return {key: tuple(items) for key, items in parsed.items()}


def _bundle(arguments: argparse.Namespace) -> DocumentBundle:
    return load_bundle(
        arguments.scenario,
        runtime_path=arguments.runtime,
        model_path=arguments.model,
        dataset_path=arguments.dataset,
        permit_path=arguments.permit,
        verification_path=arguments.verification,
        extension_schemas=load_extension_schemas(arguments.extension_schema),
    )


def _evaluator_receipts(arguments: argparse.Namespace) -> VerifiedReceiptSet:
    return load_verified_receipts(
        receipt_paths=arguments.evaluator_receipt,
        verification_paths=arguments.evaluator_verification,
        dependency_paths=arguments.evaluator_receipt_dependency,
    )


def _report_status(output: Path, status: str, key: str) -> int:
    print(json.dumps({key: str(output), "status": status}, sort_keys=True, allow_nan=False))
    return 0 if status == "passed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create-run":
            run_id = create_run_context(
                arguments.scenario,
                arguments.output,
                domains=_keyed_values(arguments.domain, "--domain"),
                time_authority=arguments.time_authority,
                time_source=arguments.time_source,
                run_id=arguments.run_id,
                extension_schemas=load_extension_schemas(arguments.extension_schema),
            )
            print(run_id)
            return 0

        if arguments.command == "doctor":
            requirements = ()
            if arguments.scenario is not None:
                scenario = load_document(arguments.scenario, expected_role="acceptance_scenario")
                requirements = scenario.data["evaluator_requirements"]
            report = doctor_report(
                mode=arguments.mode,
                evidence_dir=arguments.evidence_dir,
                measurement_complete=arguments.measurement_complete,
                evaluator_requirements=requirements,
                evaluator_receipts=_evaluator_receipts(arguments),
            )
            print(
                report_markdown(report)
                if arguments.format == "markdown"
                else json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
            )
            return 0 if report["status"] == "passed" else 1

        if arguments.command == "why":
            report = why_report(arguments.result)
            print(
                report_markdown(report)
                if arguments.format == "markdown"
                else json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
            )
            return 0

        if arguments.command == "otel-summary":
            samples = load_otlp_json_metrics(arguments.otel_metrics)
            print(
                json.dumps(
                    {
                        "sample_count": len(samples),
                        "instruments": dict(sorted(Counter(item.name for item in samples).items())),
                    },
                    sort_keys=True,
                )
            )
            return 0

        if arguments.command == "timing-check":
            scenario = load_document(
                arguments.scenario,
                expected_role="acceptance_scenario",
                extension_schemas=load_extension_schemas(arguments.extension_schema),
            )
            load_run_context(
                arguments.run_context,
                run_id=arguments.run_id,
                domain_id=arguments.domain_id,
                scenario_id=str(scenario.data["scenario_id"]),
                scenario_sha256=scenario.sha256,
            )
            evidence = load_evidence_index(
                arguments.evidence_index,
                expected_run_id=arguments.run_id,
                receipt_paths=arguments.artifact_receipt,
                verification_paths=arguments.artifact_verification,
                receipt_dependency_paths=arguments.receipt_dependency,
            )
            metrics_path = Path(arguments.otel_metrics).expanduser().resolve()
            metric_link = evidence.local_files.get(metrics_path)
            if metric_link is None or metric_link["media_type"] != OTLP_JSON_LINES_MEDIA_TYPE:
                raise ValueError(
                    f"OTLP metrics are not verified local {OTLP_JSON_LINES_MEDIA_TYPE} evidence"
                )
            samples = select_metric_points(
                load_otlp_json_metrics(
                    metrics_path,
                    expected_sha256=str(metric_link["sha256"]),
                ),
                run_id=arguments.run_id,
                domain_id=arguments.domain_id,
            )
            observation = evaluate_hardware_timing(
                scenario.data["time_policy"],
                tuple(sample for sample in samples if isinstance(sample, MetricSample)),
            )
            payload = asdict(observation)
            payload["measured_at"] = observation.measured_at.isoformat().replace("+00:00", "Z")
            print(json.dumps(payload, sort_keys=True, allow_nan=False))
            expected = arguments.expect == "within-policy"
            return 0 if observation.within_policy is expected else 1

        if arguments.command == "campaign":
            output = aggregate_campaign(
                scenario_path=arguments.scenario,
                run_context_paths=arguments.run_context,
                aggregate_paths=arguments.aggregate,
                output_path=arguments.output,
                minimum_passed_runs=arguments.minimum_passed_runs,
                maximum_failed_runs=arguments.maximum_failed_runs,
                maximum_incomplete_runs=arguments.maximum_incomplete_runs,
                maximum_error_runs=arguments.maximum_error_runs,
                campaign_id=arguments.campaign_id,
                extension_schemas=load_extension_schemas(arguments.extension_schema),
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            return _report_status(output, result["verdict"]["status"], "campaign")

        if arguments.command == "aggregate":
            output = aggregate_results(
                scenario_path=arguments.scenario,
                run_context_path=arguments.run_context,
                result_paths=arguments.result,
                output_path=arguments.output,
                transport_qualification_path=arguments.transport_qualification,
                extension_schemas=load_extension_schemas(arguments.extension_schema),
            )
            aggregate = json.loads(output.read_text(encoding="utf-8"))
            status = aggregate["cross_domain_e2e"]["status"]
            if status == "unevaluated":
                status = aggregate["per_domain_aggregate"]
            return _report_status(output, status, "aggregate")

        if arguments.command == "transport-evaluate":
            output = evaluate_transport_qualification(
                run_id=arguments.run_id,
                scenario_path=arguments.scenario,
                causal_chain_paths=arguments.causal_chain,
                channel_contract_paths=arguments.channel_contract,
                trace_paths=_keyed_values(arguments.trace, "--trace"),
                evidence_index_paths=_keyed_values(
                    arguments.evidence_index,
                    "--evidence-index",
                ),
                artifact_receipt_paths=_grouped_values(
                    arguments.artifact_receipt,
                    "--artifact-receipt",
                ),
                artifact_verification_paths=_grouped_values(
                    arguments.artifact_verification,
                    "--artifact-verification",
                ),
                receipt_dependency_paths=_grouped_values(
                    arguments.receipt_dependency,
                    "--receipt-dependency",
                ),
                clock_relation_paths=arguments.clock_relation,
                observation_output_dir=arguments.observation_output,
                output_path=arguments.output,
                extension_schemas=load_extension_schemas(arguments.extension_schema),
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            return _report_status(output, result["verdict"]["status"], "qualification")

        bundle = _bundle(arguments)
        if arguments.command == "explain":
            print(json.dumps(explain_bundle(bundle), indent=2, sort_keys=True, allow_nan=False))
            return 0

        evaluator_receipts = _evaluator_receipts(arguments)

        if arguments.command == "evaluate":
            outputs = evaluate_from_evidence(
                run_id=arguments.run_id,
                domain_id=arguments.domain_id,
                run_context_path=arguments.run_context,
                bundle=bundle,
                evidence_index_path=arguments.evidence_index,
                artifact_receipt_paths=arguments.artifact_receipt,
                artifact_verification_paths=arguments.artifact_verification,
                receipt_dependency_paths=arguments.receipt_dependency,
                evaluator_receipts=evaluator_receipts,
                otel_metrics_path=arguments.otel_metrics,
                window_start_ns=arguments.window_start_ns,
                window_end_ns=arguments.window_end_ns,
                output_dir=arguments.output,
            )
        else:
            outputs = run_verification(
                run_id=arguments.run_id,
                domain_id=arguments.domain_id,
                run_context_path=arguments.run_context,
                bundle=bundle,
                evidence_index_path=arguments.evidence_index,
                artifact_receipt_paths=arguments.artifact_receipt,
                artifact_verification_paths=arguments.artifact_verification,
                receipt_dependency_paths=arguments.receipt_dependency,
                evaluator_receipts=evaluator_receipts,
                otel_metrics_path=arguments.otel_metrics,
                measurement_complete_path=arguments.measurement_complete,
                output_dir=arguments.output,
            )
        print(
            json.dumps(
                {
                    "status": outputs.result["status"],
                    "result": str(outputs.result_path),
                    "junit": str(outputs.junit_path),
                },
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 0 if outputs.result["status"] == "passed" else 1
    except (OSError, RuntimeError, ValueError) as error:
        diagnostic_output = getattr(arguments, "diagnostic_output", None)
        if diagnostic_output:
            write_error_diagnostic(
                diagnostic_output,
                command=str(arguments.command),
                error=error,
            )
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

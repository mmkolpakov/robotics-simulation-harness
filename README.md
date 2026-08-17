# Robotics Acceptance Harness

[![CI](https://github.com/mmkolpakov/robotics-acceptance-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/mmkolpakov/robotics-acceptance-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Attach-only acceptance testing for existing ROS 2 executions.

The harness validates an execution bundle, observes the declared ROS graph and
OpenTelemetry data, verifies retained evidence, and writes contract-valid JSON
and JUnit results. It does not launch workloads, control simulators, change node
lifecycle states, execute cryptographic signature tools, or publish commands to equipment.
It verifies externally produced signature results and their digest chain; the
signature tool itself remains an infrastructure responsibility.

## Architecture

```text
product workload -> runtime infrastructure -> running ROS 2 graph
                                                   |
runtime contracts -> acceptance harness -----------+
                          |
                          +-> result JSON + JUnit + evidence links
```

- [robotics-runtime-contracts](https://github.com/mmkolpakov/robotics-runtime-contracts)
  owns document structure and verdict semantics.
- This repository owns observation and evaluation.
- [robotics-runtime-infra](https://github.com/mmkolpakov/robotics-runtime-infra)
  owns runtime, simulator, middleware, recorder, and hardware provider adapters.
- Product repositories own scenes, robots, models, behavior, and business
  evaluators.

The harness consumes provider-neutral runtime facts. Adding a simulator,
middleware, recorder, or accelerator does not require a new scenario or result
version.

## Requirements

| Component | Baseline |
| --- | --- |
| Python | 3.12 or 3.13 |
| Contracts | `robotics-runtime-contracts>=0.16,<0.17` |
| ROS observation | ROS 2 Jazzy packages in the observer environment |
| Metrics | OTLP JSON Lines exported by OpenTelemetry Collector |

All public contract families currently use one canonical `v1`. This is a
pre-1.0 line; compatibility starts with the first stable release.

## Install

Development uses the exact contracts revision recorded in `uv.lock`:

```bash
git clone https://github.com/mmkolpakov/robotics-acceptance-harness.git
cd robotics-acceptance-harness
uv sync --locked --all-groups
uv run robotics-acceptance --version
```

Release consumers should install the published wheel together with the locked
contracts wheel and verify release provenance as described in
[`docs/supply-chain.md`](docs/supply-chain.md).

## Quick Start

Validate and cross-check a known-good bundle without ROS:

```bash
uv run robotics-acceptance explain \
  --scenario tests/fixtures/simulation/scenario.yaml \
  --runtime tests/fixtures/simulation/runtime.yaml
```

Create the immutable context shared by every domain in one run:

```bash
robotics-acceptance create-run \
  --scenario scenario.yaml \
  --output acceptance-run.json \
  --domain primary=observer \
  --time-authority sim_clock \
  --time-source simulation-clock
```

Run `robotics-acceptance COMMAND --help` for the complete option set.

## Commands

| Command | Purpose | Controls the workload |
| --- | --- | --- |
| `create-run` | Create an immutable run context | No |
| `explain` | Validate and explain an execution bundle | No |
| `verify` | Observe a live ROS 2 execution | No |
| `evaluate` | Re-evaluate finalized evidence offline | No |
| `aggregate` | Fold all declared domain results | No |
| `transport-evaluate` | Qualify cross-domain delivery and causal traces | No |
| `campaign` | Aggregate repeated run verdicts | No |
| `doctor` | Report observer and extension metadata | No |
| `why` | Explain a result verdict | No |
| `timing-check` | Check verified clock metrics against scenario policy | No |
| `otel-summary` | Summarize normalized OTLP metric points | No |

Successful acceptance returns `0`, a completed non-passing verdict returns `1`,
and invalid input or an observation failure returns `2`.

## Live Observation

Runtime infrastructure starts the workload, recorder, and telemetry collector.
The harness joins the existing ROS domain:

```bash
robotics-acceptance verify \
  --scenario scenario.yaml \
  --runtime runtime-manifest.json \
  --run-id run-7dd792f2-4f75-4f4d-81b0-48c8c2a8f76c \
  --domain-id primary \
  --run-context acceptance-run.json \
  --evidence-index evidence-index.json \
  --otel-metrics metrics.otlp.jsonl \
  --measurement-complete measurement-complete \
  --output results
```

The output directory contains `acceptance-result.json` and `junit.xml`. The
observer inherits standard ROS variables such as `ROS_DOMAIN_ID`,
`RMW_IMPLEMENTATION`, and the SROS2 environment. It has no private fallback for
document paths or execution identity.

## Offline Evaluation

`evaluate` runs the same metric, evidence, and product evaluators without
joining ROS. Live graph, clock, safety-boundary, and shutdown observations are
marked `unevaluated`, so an offline result cannot silently claim complete live
acceptance.

Local evidence is verified by URI, path, size, and SHA-256. Retained evidence
also requires a receipt, its typed external-verification record, and the
referenced statement, trust policy, and verification evidence:

```bash
robotics-acceptance evaluate \
  --scenario scenario.yaml --runtime runtime-manifest.json \
  --run-id "$RUN_ID" --domain-id primary --run-context acceptance-run.json \
  --evidence-index evidence-index.json \
  --artifact-receipt artifact-receipt.json \
  --artifact-verification artifact-verification.json \
  --receipt-dependency statement.json \
  --receipt-dependency trust-policy.json \
  --receipt-dependency verification.bundle \
  --otel-metrics metrics.otlp.jsonl \
  --window-start-ns 1786000000000000000 \
  --window-end-ns 1786000030000000000 \
  --output results
```

The external verification binds the full artifact descriptor: URI, immutable
revision, media type, size, and SHA-256. The harness needs no storage
credentials. Upload, signing, and retention lifecycle remain infrastructure
responsibilities. OTLP file-exporter streams use `application/x-ndjson`.

## Contract Inputs

| Input | Contract role |
| --- | --- |
| Scenario | `acceptance-scenario.v1` |
| Runtime facts | `runtime-manifest.v1` |
| Run context | `acceptance-run.v1` |
| Evidence and provenance | `evidence-index.v1`, `artifact-receipt.v1`, `artifact-verification.v1` |
| Model and dataset provenance | `model-artifact-manifest.v1`, `dataset-manifest.v1` |
| Physical authorization | `execution-permit.v1`, `execution-verification.v1` |
| Transport inputs | `transport-channel.v1`, `clock-relation.v1`, `causal-chain.v1` |
| Outputs | `acceptance-result.v1`, `acceptance-aggregate.v1`, `campaign-summary.v1` |

Scenario extensions are explicit and digest-pinned. Pass the same
`--extension-schema URI=PATH` mapping to every command that reads the scenario.
Extensions cannot replace common safety, timing, transport, or evidence rules.

## Product Evaluators

Product packages register standard PyPA entry points:

```toml
[project.entry-points."robotics_acceptance.evaluators"]
"org.example.sorting" = "sorting_acceptance:evaluate"
```

The scenario and runtime must declare the same namespace, target, distribution,
version, wheel SHA-256, and receipt SHA-256. Before importing the target, the
harness verifies the released wheel's receipt and provenance chain. Separately,
it verifies every installed file hash declared by the environment's `RECORD`;
that installation belongs to the observed execution-subject image. PEP 610
metadata and an installed `RECORD` are not treated as proof of released wheel
identity. Unhashed bytecode and module origins outside that `RECORD` fail
closed; evaluator images should install with bytecode generation disabled.

An evaluator receives an immutable `EvaluationContext` and returns
`AssertionEvaluation` objects in its own namespace. Every product assertion
must reference at least one digest from verified evidence. Duplicate assertion
IDs, undeclared packages, foreign namespaces, and unknown evidence fail closed.

`doctor --scenario scenario.yaml` checks the required evaluator metadata,
receipt chain, and installed `RECORD` without importing evaluator code.

## Pytest Integration

```bash
uv run pytest \
  -p robotics_acceptance_harness.plugin \
  --robotics-scenario scenario.yaml \
  --robotics-runtime runtime-manifest.json
```

Use `robotics_bundle` for the validated immutable bundle and
`robotics_scenario` for the scenario mapping. The plugin is not activated by
package installation and refuses physical targets.

## Development

```bash
uv sync --locked --all-groups
uv run pre-commit run --all-files
uv run coverage run --branch -m pytest
uv run coverage report --fail-under=80
uv build --no-sources
```

See [compatibility](docs/compatibility.md), [architecture decisions](docs/decisions/README.md),
[supply-chain assurance](docs/supply-chain.md), and the
[REP-2004 quality declaration](QUALITY_DECLARATION.md). Security reports follow
[SECURITY.md](SECURITY.md).

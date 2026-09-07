from __future__ import annotations

import json
from pathlib import Path

import pytest

from robotics_acceptance_harness.metrics import HistogramSample, MetricSample
from robotics_acceptance_harness.otel import MetricInputError, load_otlp_json_metrics


@pytest.mark.parametrize(
    "payload",
    [
        '{"unknownField": true}',
        '{"resourceMetrics": "invalid"}',
        "null",
        "42",
        "[]",
        '""',
        "false",
        '{"resourceMetrics": [[]]}',
        '{"resource_metrics": [{"resource": ""}]}',
        '{"resourceMetrics": [{"scopeMetrics": [{"metrics": [[]]}]}]}',
        '{"resourceMetrics": [}',
    ],
)
def test_rejects_malformed_otlp_with_source_line(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text("{}\n" + payload + "\n", encoding="utf-8")

    with pytest.raises(MetricInputError, match=r"invalid\.jsonl:2:") as caught:
        load_otlp_json_metrics(path)

    assert caught.value.__cause__ is not None


def test_loads_standard_otlp_json_number_points(tmp_path: Path) -> None:
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "robotics.clock.sync_protocol",
                            "value": {"stringValue": "ptp"},
                        }
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "robotics.message.age",
                                "unit": "ms",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": "1000000000",
                                            "asDouble": 12.5,
                                            "attributes": [
                                                {
                                                    "key": "robotics.clock.source",
                                                    "value": {"stringValue": "pmc"},
                                                }
                                            ],
                                        }
                                    ]
                                },
                            },
                            {
                                "name": "robotics.message.lost",
                                "unit": "1",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": "1000000000",
                                            "asInt": "0",
                                        }
                                    ],
                                },
                            },
                        ]
                    }
                ],
            }
        ]
    }
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    samples = load_otlp_json_metrics(path)

    assert all(isinstance(sample, MetricSample) for sample in samples)
    scalar_samples = [sample for sample in samples if isinstance(sample, MetricSample)]
    assert [(sample.name, sample.value, sample.unit) for sample in scalar_samples] == [
        ("robotics.message.age", 12.5, "ms"),
        ("robotics.message.lost", 0.0, "1"),
    ]
    assert scalar_samples[0].attributes == {
        "robotics.clock.sync_protocol": "ptp",
        "robotics.clock.source": "pmc",
    }
    assert scalar_samples[1].attributes == {"robotics.clock.sync_protocol": "ptp"}
    assert scalar_samples[1].instrument_kind == "sum"
    assert scalar_samples[1].temporality == "cumulative"
    assert scalar_samples[1].monotonic


def test_loads_explicit_bucket_histogram_without_expanding_events(tmp_path: Path) -> None:
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "domain.id", "value": {"stringValue": "camera"}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "robotics.message.age",
                                "unit": "ms",
                                "histogram": {
                                    "aggregationTemporality": 1,
                                    "dataPoints": [
                                        {
                                            "startTimeUnixNano": "100",
                                            "timeUnixNano": "200",
                                            "count": "50000",
                                            "sum": 125000.0,
                                            "min": 0.1,
                                            "max": 12.5,
                                            "explicitBounds": [1, 5, 10],
                                            "bucketCounts": ["10000", "25000", "14000", "1000"],
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    samples = load_otlp_json_metrics(path)

    assert len(samples) == 1
    histogram = samples[0]
    assert isinstance(histogram, HistogramSample)
    assert histogram.temporality == "delta"
    assert histogram.count == 50_000
    assert histogram.bucket_counts == (10_000, 25_000, 14_000, 1_000)
    assert histogram.explicit_bounds == (1, 5, 10)
    assert histogram.sum == 125_000
    assert histogram.min == 0.1
    assert histogram.max == 12.5
    assert histogram.attributes == {"domain.id": "camera"}


def test_invalid_otlp_json_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text("{}\nnot-json\n", encoding="utf-8")

    with pytest.raises(MetricInputError, match=":2"):
        load_otlp_json_metrics(path)


def test_otlp_metrics_are_parsed_only_when_digest_matches(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(MetricInputError, match="digest differs"):
        load_otlp_json_metrics(path, expected_sha256="0" * 64)

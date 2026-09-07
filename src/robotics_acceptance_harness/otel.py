from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from google.protobuf.descriptor import Descriptor
from google.protobuf.json_format import ParseDict, ParseError
from google.protobuf.message import Message
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from robotics_acceptance_harness.metrics import (
    HistogramSample,
    MetricAttribute,
    MetricPoint,
    MetricSample,
    MetricTemporality,
)

OTLP_JSON_LINES_MEDIA_TYPE = "application/x-ndjson"


class MetricInputError(ValueError):
    """Raised when an OTLP JSON file cannot be interpreted as metric samples."""


def _require_message_objects(value: object, descriptor: Descriptor, path: str) -> dict[str, Any]:
    """Reject non-object messages that ParseDict can silently treat as empty."""
    if not isinstance(value, dict):
        raise TypeError(f"{path}: OTLP message must be a JSON object")
    fields = {field.json_name: field for field in descriptor.fields}
    fields.update(descriptor.fields_by_name)
    for name, item in value.items():
        field = fields.get(name)
        if field is None or item is None or field.message_type is None:
            continue  # ParseDict handles unknown fields, scalars and field-level nulls.
        field_path = f"{path}.{name}"
        repeated = (
            field.is_repeated
            if hasattr(field, "is_repeated")
            else cast(Any, field).label == field.LABEL_REPEATED
        )
        if repeated:
            if isinstance(item, list):
                for index, child in enumerate(item):
                    _require_message_objects(child, field.message_type, f"{field_path}[{index}]")
        else:
            _require_message_objects(item, field.message_type, field_path)
    return value


def parse_otlp_request[RequestT: Message](payload: object, request: RequestT) -> RequestT:
    """Parse an OTLP request using protobuf, with strict message object shapes."""
    # Native and pure-Python descriptors expose the same reflection API.
    document = _require_message_objects(payload, cast(Descriptor, request.DESCRIPTOR), "$")
    return ParseDict(document, request)


def read_otlp_json_lines(
    path: str | Path,
    expected_sha256: str | None,
    error_type: type[ValueError],
) -> tuple[Path, list[str]]:
    """Read and integrity-check newline-delimited OTLP JSON."""

    source = Path(path).expanduser().resolve()
    try:
        payload_bytes = source.read_bytes()
    except OSError as error:
        raise error_type(f"cannot read {source}: {error}") from error
    observed_sha256 = sha256(payload_bytes).hexdigest()
    if expected_sha256 is not None and observed_sha256 != expected_sha256:
        raise error_type(
            f"{source} digest differs: expected {expected_sha256}; observed {observed_sha256}"
        )
    try:
        return source, payload_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise error_type(f"cannot decode {source} as UTF-8: {error}") from error


def _number_value(point: Any) -> float | None:
    value_kind = point.WhichOneof("value")
    if value_kind == "as_double":
        return float(point.as_double)
    if value_kind == "as_int":
        return float(point.as_int)
    return None


def _temporality(value: int) -> MetricTemporality:
    if value == 1:
        return "delta"
    if value == 2:
        return "cumulative"
    return "unspecified"


def _optional_number(point: Any, field_name: str) -> float | None:
    try:
        present = point.HasField(field_name)
    except ValueError:
        present = False
    return float(getattr(point, field_name)) if present else None


def _has_recorded_value(point: Any) -> bool:
    return int(point.flags) & 1 == 0


def otlp_attribute_value(value: Any) -> MetricAttribute | None:
    value_kind = value.WhichOneof("value")
    if value_kind == "string_value":
        return str(value.string_value)
    if value_kind == "bool_value":
        return bool(value.bool_value)
    if value_kind == "int_value":
        return int(value.int_value)
    if value_kind == "double_value":
        return float(value.double_value)
    return None


def otlp_attributes(items: Any) -> dict[str, MetricAttribute]:
    attributes: dict[str, MetricAttribute] = {}
    for item in items:
        value = otlp_attribute_value(item.value)
        if value is not None:
            attributes[str(item.key)] = value
    return attributes


def load_otlp_json_metrics(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[MetricPoint, ...]:
    """Read newline-delimited OTLP JSON emitted by the Collector file exporter."""

    source, lines = read_otlp_json_lines(path, expected_sha256, MetricInputError)
    samples: list[MetricPoint] = []

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            request = parse_otlp_request(payload, ExportMetricsServiceRequest())
        except (ParseError, TypeError, ValueError) as error:
            raise MetricInputError(
                f"invalid OTLP JSON at {source}:{line_number}: {error}"
            ) from error

        for resource_metrics in request.resource_metrics:
            resource_attributes = otlp_attributes(resource_metrics.resource.attributes)
            for scope_metrics in resource_metrics.scope_metrics:
                scope_attributes = {
                    **resource_attributes,
                    **otlp_attributes(scope_metrics.scope.attributes),
                }
                for metric in scope_metrics.metrics:
                    data_kind = metric.WhichOneof("data")
                    if data_kind not in {"gauge", "sum", "histogram"}:
                        continue
                    data = getattr(metric, data_kind)
                    temporality = (
                        _temporality(int(data.aggregation_temporality))
                        if data_kind in {"sum", "histogram"}
                        else None
                    )
                    for point in data.data_points:
                        if not _has_recorded_value(point):
                            continue
                        attributes = {
                            **scope_attributes,
                            **otlp_attributes(point.attributes),
                        }
                        if data_kind == "histogram":
                            try:
                                samples.append(
                                    HistogramSample(
                                        name=metric.name,
                                        unit=metric.unit,
                                        observed_at_ns=int(point.time_unix_nano),
                                        count=int(point.count),
                                        bucket_counts=tuple(
                                            int(value) for value in point.bucket_counts
                                        ),
                                        explicit_bounds=tuple(
                                            float(value) for value in point.explicit_bounds
                                        ),
                                        attributes=attributes,
                                        temporality=temporality or "unspecified",
                                        start_time_ns=int(point.start_time_unix_nano),
                                        sum=_optional_number(point, "sum"),
                                        min=_optional_number(point, "min"),
                                        max=_optional_number(point, "max"),
                                    )
                                )
                            except ValueError as error:
                                raise MetricInputError(
                                    f"invalid OTLP histogram at {source}:{line_number}: {error}"
                                ) from error
                            continue
                        value = _number_value(point)
                        if value is None:
                            continue
                        try:
                            samples.append(
                                MetricSample(
                                    name=metric.name,
                                    value=value,
                                    unit=metric.unit,
                                    observed_at_ns=int(point.time_unix_nano),
                                    attributes=attributes,
                                    instrument_kind=data_kind,
                                    temporality=temporality,
                                    start_time_ns=int(point.start_time_unix_nano),
                                    monotonic=bool(data.is_monotonic)
                                    if data_kind == "sum"
                                    else False,
                                )
                            )
                        except ValueError as error:
                            raise MetricInputError(
                                f"invalid OTLP metric at {source}:{line_number}: {error}"
                            ) from error
    return tuple(samples)


def select_metric_points(
    samples: Sequence[MetricPoint],
    *,
    run_id: str,
    domain_id: str,
) -> tuple[MetricPoint, ...]:
    """Select points attributed to one run and logical domain."""

    return tuple(
        sample
        for sample in samples
        if sample.attributes.get("run.id") == run_id
        and sample.attributes.get("domain.id") == domain_id
    )

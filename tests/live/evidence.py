from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from robotics_runtime_contracts import validate_document

from tests.support import local_evidence_artifact

QUALIFICATION_GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "qualification-golden.json").read_text(encoding="utf-8")
)

QUALIFICATION_METRICS = {
    "robotics.message.age": ("ms", "histogram"),
    "robotics.message.received": ("{message}", "sum"),
    "robotics.message.lost": ("{message}", "sum"),
    "robotics.message.sequence_error": ("{message}", "sum"),
    "robotics.time_authority.delivery_latency": ("ms", "histogram"),
}


def qualification_metrics(start_ns: int, end_ns: int) -> list[dict]:
    """Synthetic delta fixtures, covering the real CLI window at 10 ms resolution.

    These values test aggregation and attribution, not DDS performance. The
    fixture spans process startup through measurement completion; the harness
    selects the intervals inside its own observed window without clock injection.
    """
    interval_ns = QUALIFICATION_GOLDEN["interval_ns"]
    count = QUALIFICATION_GOLDEN["histogram_count"]
    bounds = QUALIFICATION_GOLDEN["explicit_bounds_ms"]
    intervals = tuple(range(start_ns, end_ns - interval_ns, interval_ns))
    metrics = []
    for name, (unit, kind) in QUALIFICATION_METRICS.items():
        points = []
        for beginning in intervals:
            point = {
                "startTimeUnixNano": str(beginning),
                "timeUnixNano": str(beginning + interval_ns),
            }
            if kind == "histogram":
                # Three explicit synthetic observations per delta interval.
                key = "message_age_ms" if name == "robotics.message.age" else "delivery_latency_ms"
                value = QUALIFICATION_GOLDEN[key]
                buckets = [0] * (len(bounds) + 1)
                bucket_index = next(
                    (i for i, bound in enumerate(bounds) if value <= bound), len(bounds)
                )
                buckets[bucket_index] = count
                point.update(
                    count=str(count),
                    sum=value * count,
                    min=value,
                    max=value,
                    explicitBounds=bounds,
                    bucketCounts=[str(bucket) for bucket in buckets],
                )
            else:
                point.update(
                    asInt=str(QUALIFICATION_GOLDEN["counters"][name]),
                    attributes=[
                        {
                            "key": "sequence.measurement.method",
                            "value": {"stringValue": "rmw_publication_sequence_single_publisher"},
                        }
                    ],
                )
            points.append(point)
        instrument = {"aggregationTemporality": 1, "dataPoints": points}
        if kind == "sum":
            instrument["isMonotonic"] = True
        metrics.append({"name": name, "unit": unit, kind: instrument})
    return metrics


def clock_recording(directory: Path, samples: tuple[tuple[int, bytes], ...]) -> dict:
    """Write actual received CDR Clock messages and derive the summary from MCAP."""
    from mcap.reader import make_reader
    from mcap.writer import CompressionType, Writer

    assert len(samples) >= 30, "need at least 30 actual received /clock messages"
    path = directory / "clock.mcap"
    with path.open("wb") as output:
        writer = Writer(output, compression=CompressionType.ZSTD)
        writer.start(profile="ros2", library="harness-live-test")
        schema = writer.register_schema(
            name="rosgraph_msgs/msg/Clock",
            encoding="ros2msg",
            data=(
                b"builtin_interfaces/Time clock\n"
                b"================================================================================\n"
                b"MSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
            ),
        )
        channel = writer.register_channel(topic="/clock", message_encoding="cdr", schema_id=schema)
        for sequence, (timestamp, data) in enumerate(samples):
            writer.add_message(
                channel, log_time=timestamp, publish_time=timestamp, data=data, sequence=sequence
            )
        writer.finish()
    with path.open("rb") as source:
        reader = make_reader(source, validate_crcs=True)
        observed = reader.get_summary()
        assert observed is not None and observed.statistics is not None
        assert sum(1 for _ in reader.iter_messages()) == len(samples)
    statistics = observed.statistics
    artifact = local_evidence_artifact(
        path,
        media_type="application/mcap",
        artifact_index=1,
        overrides={"kind": "recording"},
    )
    summary = {
        "schema_version": "recording-summary.v1",
        "source_sha256": artifact["sha256"],
        "statistics": {
            field: getattr(statistics, field)
            for field in (
                "message_count",
                "schema_count",
                "channel_count",
                "attachment_count",
                "metadata_count",
                "chunk_count",
                "message_start_time",
                "message_end_time",
            )
        },
        "compressions": sorted({chunk.compression for chunk in observed.chunk_indexes}),
        "channels": [
            {
                "topic": item.topic,
                "message_encoding": item.message_encoding,
                "schema_name": observed.schemas[item.schema_id].name,
                "message_count": statistics.channel_message_counts[item.id],
            }
            for item in observed.channels.values()
        ],
    }
    for key in ("message_start_time", "message_end_time"):
        summary["statistics"][f"{key}_ns"] = summary["statistics"].pop(key)
    validate_document(summary)
    summary_path = directory / "clock.recording-summary.json"
    summary_path.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
    artifact["recording_summary"] = {
        "uri": summary_path.as_uri(),
        "sha256": sha256(summary_path.read_bytes()).hexdigest(),
        "size_bytes": summary_path.stat().st_size,
    }
    return artifact

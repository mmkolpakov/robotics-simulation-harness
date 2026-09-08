from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml


class Collector:
    """Run the actual Collector file exporter; never write its output ourselves."""

    def __init__(self, directory: Path) -> None:
        binary = os.environ.get("OTELCOL_BINARY", "otelcol-contrib")
        executable = shutil.which(binary)
        if executable is None:
            raise RuntimeError(f"live CLI test requires a real Collector binary: {binary}")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/v1/metrics"
        self.metrics = directory / "metrics.collector.otlp.jsonl"
        if self.metrics.exists():
            raise RuntimeError(f"use an empty artifact directory: {self.metrics}")
        self.log_path = directory / "collector.log"
        self.config_path = directory / "collector.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "receivers": {
                        "otlp": {"protocols": {"http": {"endpoint": f"127.0.0.1:{port}"}}}
                    },
                    "exporters": {
                        "file": {
                            "path": str(self.metrics),
                            "format": "json",
                            "flush_interval": "100ms",
                        }
                    },
                    "service": {
                        "telemetry": {"metrics": {"level": "none"}},
                        "pipelines": {"metrics": {"receivers": ["otlp"], "exporters": ["file"]}},
                    },
                }
            ),
            encoding="utf-8",
        )
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=True, timeout=10
        )
        (directory / "collector-version.txt").write_text(version.stdout, encoding="utf-8")
        self.log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [executable, "--config", str(self.config_path)],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

    def send(self, payload: dict) -> None:
        request = Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        deadline = monotonic() + 10
        while True:
            if self.process.poll() is not None:
                raise RuntimeError(self.log_path.read_text(encoding="utf-8"))
            try:
                with urlopen(request, timeout=2) as response:
                    assert response.status == 200
                    reply = json.loads(response.read() or b"{}")
                    assert not reply.get("partialSuccess"), reply
                return
            except URLError:
                if monotonic() >= deadline:
                    raise
                sleep(0.05)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise
        self.log.close()
        assert self.process.returncode == 0, self.log_path.read_text(encoding="utf-8")
        assert self.metrics.is_file() and self.metrics.stat().st_size > 0

    def __enter__(self) -> Collector:
        return self

    def __exit__(self, *_args) -> None:
        self.stop()

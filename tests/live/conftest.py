from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_ros: requires real ROS 2 Jazzy (explicit opt-in)")


@pytest.fixture(autouse=True)
def require_live_ros() -> None:
    if os.environ.get("ROBOTICS_LIVE_ROS") != "1":
        pytest.skip("set ROBOTICS_LIVE_ROS=1 to run the ROS 2 Jazzy integration tests")
    if os.environ.get("ROS_DISTRO") != "jazzy":
        pytest.fail("live tests require ROS_DISTRO=jazzy; source /opt/ros/jazzy/setup.bash")
    for name in (
        "rclpy",
        "rclpy.lifecycle",
        "rosidl_runtime_py.utilities",
        "rosgraph_msgs.msg",
        "lifecycle_msgs.srv",
        "std_msgs.msg",
        "example_interfaces.srv",
        "action_tutorials_interfaces.action",
    ):
        try:
            importlib.import_module(name)
        except ImportError as error:
            pytest.fail(f"live ROS dependency {name} is unavailable: {error}")


@pytest.fixture
def live_graph(require_live_ros: None):
    # Import only after the opt-in check; ordinary unit collection needs no ROS.
    from tests.live.graph import LiveGraph

    with LiveGraph() as graph:
        yield graph


@pytest.fixture
def live_output(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    root = os.environ.get("ROBOTICS_LIVE_ARTIFACTS")
    output = Path(root) / request.node.name if root else tmp_path
    output.mkdir(parents=True, exist_ok=True)
    return output.resolve()


@pytest.fixture
def collector(live_output: Path, require_live_ros: None) -> Iterator:
    from tests.live.collector import Collector

    with Collector(live_output) as instance:
        yield instance

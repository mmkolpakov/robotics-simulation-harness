from __future__ import annotations

from collections.abc import Callable
from threading import Event
from types import SimpleNamespace

import pytest

from robotics_acceptance_harness.readiness import evaluate_graph
from robotics_acceptance_harness.ros import RosGraphObserver, RosObserverError


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def try_shutdown(self) -> None:
        self.closed = True


class FakeFuture:
    def done(self) -> bool:
        return True

    def result(self) -> object:
        return SimpleNamespace(current_state=SimpleNamespace(label="active"))


class FakeClient:
    def service_is_ready(self) -> bool:
        return True

    def call_async(self, _request: object) -> FakeFuture:
        return FakeFuture()


class FakeNode:
    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[object], None]] = {}
        self.subscription_qos: dict[str, object] = {}
        self.destroyed = False
        self.executor_started = Event()
        self.executor_stopped = Event()
        self.shutdown_results: list[bool] = []

    def create_subscription(
        self,
        _message_type: object,
        topic: str,
        callback: Callable[[object], None],
        qos: object,
    ) -> object:
        self.callbacks[topic] = callback
        self.subscription_qos[topic] = qos
        return object()

    def create_client(self, _service_type: object, _name: str) -> FakeClient:
        return FakeClient()

    def get_topic_names_and_types(self) -> list[tuple[str, list[str]]]:
        return [
            ("/camera/image", ["sensor_msgs/msg/Image"]),
            ("/clock", ["rosgraph_msgs/msg/Clock"]),
        ]

    def count_subscribers(self, name: str) -> int:
        return 2 if name == "/camera/image" else 1

    def count_publishers(self, _name: str) -> int:
        return 1

    def get_publishers_info_by_topic(self, _name: str) -> list[object]:
        return [SimpleNamespace(qos_profile="sensor-data")]

    def get_service_names_and_types(self) -> list[tuple[str, list[str]]]:
        return [("/camera/get_parameters", ["rcl_interfaces/srv/GetParameters"])]

    def get_name(self) -> str:
        return "robotics_acceptance_observer"

    def get_namespace(self) -> str:
        return "/"

    def get_node_names_and_namespaces(self) -> list[tuple[str, str]]:
        return [("application", "/"), ("robotics_acceptance_observer", "/")]

    def get_client_names_and_types_by_node(
        self,
        node_name: str,
        _node_namespace: str,
    ) -> list[tuple[str, list[str]]]:
        if node_name == "robotics_acceptance_observer":
            return [("/camera/get_state", ["lifecycle_msgs/srv/GetState"])]
        return [("/camera/get_parameters", ["rcl_interfaces/srv/GetParameters"])]

    def get_service_names_and_types_by_node(
        self,
        _node_name: str,
        _node_namespace: str,
    ) -> list[tuple[str, list[str]]]:
        return [
            ("/camera/get_parameters", ["rcl_interfaces/srv/GetParameters"]),
            ("/arm", ["example_interfaces/srv/Trigger"]),
        ]

    def destroy_node(self) -> None:
        self.destroyed = True


class FakeExecutor:
    def __init__(self, *, context: FakeContext) -> None:
        self.context = context
        self.node: FakeNode | None = None

    def add_node(self, node: FakeNode) -> None:
        self.node = node

    def spin(self) -> None:
        assert self.node is not None
        for topic, callback in self.node.callbacks.items():
            message = (
                SimpleNamespace(clock=SimpleNamespace(sec=1, nanosec=2))
                if topic == "/clock"
                else object()
            )
            callback(message)
        self.node.executor_started.set()
        self.node.executor_stopped.wait()

    def shutdown(self, timeout_sec: float) -> bool:
        assert timeout_sec == 5.0
        assert self.node is not None
        result = self.node.shutdown_results.pop(0) if self.node.shutdown_results else True
        if not result:
            return False
        self.node.executor_stopped.set()
        return True

    def remove_node(self, node: FakeNode) -> None:
        assert node is self.node


class GetState:
    class Request:
        pass


def expected_graph() -> dict[str, object]:
    return {
        "topics": [
            {
                "name": "/camera/image",
                "type": "sensor_msgs/msg/Image",
                "min_publishers": 1,
                "min_subscribers": 1,
                "first_message_timeout_sec": 2,
                "qos_profile": "sensor_data",
            }
        ],
        "services": [
            {
                "name": "/camera/get_parameters",
                "type": "rcl_interfaces/srv/GetParameters",
                "server_required": True,
            }
        ],
        "actions": [
            {
                "name": "/takeoff",
                "type": "example_interfaces/action/Fibonacci",
                "server_required": True,
            }
        ],
        "lifecycle_nodes": [
            {
                "name": "/camera",
                "required_state": "active",
                "timeout_sec": 2,
                "stable_for_sec": 0,
            }
        ],
    }


def fake_modules(node: FakeNode) -> dict[str, object]:
    return {
        "rclpy": SimpleNamespace(
            init=lambda **_kwargs: None,
            create_node=lambda *_args, **_kwargs: node,
        ),
        "rclpy.action": SimpleNamespace(
            get_action_server_names_and_types_by_node=lambda observed_node, *_args: (
                [("/takeoff", ["example_interfaces/action/Fibonacci"])]
                if observed_node is node
                else []
            ),
            get_action_client_names_and_types_by_node=lambda observed_node, *_args: (
                [("/land", ["example_interfaces/action/Fibonacci"])]
                if observed_node is node
                else []
            ),
        ),
        "rclpy.context": SimpleNamespace(Context=FakeContext),
        "rclpy.executors": SimpleNamespace(SingleThreadedExecutor=FakeExecutor),
        "rclpy.qos": SimpleNamespace(
            qos_profile_system_default="system-default",
            qos_profile_sensor_data="sensor-data",
            qos_profile_services_default="services-default",
            qos_profile_parameters="parameters",
            QoSProfile=SimpleNamespace,
            ReliabilityPolicy=SimpleNamespace(BEST_EFFORT="best-effort"),
            DurabilityPolicy=SimpleNamespace(VOLATILE="volatile"),
            QoSCompatibility=SimpleNamespace(ERROR="error"),
            qos_check_compatible=lambda *_args: ("ok", ""),
        ),
        "rosidl_runtime_py.utilities": SimpleNamespace(get_message=lambda name: name),
        "lifecycle_msgs.srv": SimpleNamespace(GetState=GetState),
        "rosgraph_msgs.msg": SimpleNamespace(Clock=object),
    }


def test_ros_observer_fails_clearly_outside_ros_runtime() -> None:
    def missing_module(name: str) -> object:
        raise ModuleNotFoundError(name)

    with pytest.raises(RosObserverError, match="must be available"):
        RosGraphObserver(
            {"topics": [], "services": [], "actions": [], "lifecycle_nodes": []},
            observe_clock=True,
            module_loader=missing_module,
        )


def test_ros_observer_reports_graph_clock_and_lifecycle_without_writing() -> None:
    node = FakeNode()
    modules = fake_modules(node)
    observer = RosGraphObserver(
        expected_graph(),
        observe_clock=True,
        module_loader=modules.__getitem__,
    )

    assert node.executor_started.wait(timeout=1.0)
    assert node.subscription_qos["/clock"] == SimpleNamespace(
        depth=1, reliability="best-effort", durability="volatile"
    )
    assert observer.clock_samples == ()
    observer.start_clock_observation()
    node.callbacks["/clock"](SimpleNamespace(clock=SimpleNamespace(sec=1, nanosec=2)))
    observer.stop_clock_observation()
    assert observer.clock_samples[-1].source_time_ns == 1_000_000_002
    node.callbacks["/clock"](SimpleNamespace(clock=SimpleNamespace(sec=2, nanosec=0)))
    assert len(observer.clock_samples) == 1
    observer.snapshot()
    snapshot = observer.snapshot()

    assert evaluate_graph(expected_graph(), snapshot) == ()
    assert snapshot.topics["/camera/image"].subscribers == 1
    assert snapshot.lifecycle_nodes["/camera"].state == "active"
    observer.close()
    observer.close()
    assert node.executor_stopped.is_set()
    assert node.destroyed
    with pytest.raises(RosObserverError, match="closed"):
        observer.snapshot()


def test_ros_observer_bounds_unique_clock_samples() -> None:
    node = FakeNode()
    observer = RosGraphObserver(
        expected_graph(),
        observe_clock=True,
        module_loader=fake_modules(node).__getitem__,
        max_clock_samples=1,
    )

    assert node.executor_started.wait(timeout=1.0)
    observer.start_clock_observation()
    node.callbacks["/clock"](SimpleNamespace(clock=SimpleNamespace(sec=1, nanosec=0)))
    node.callbacks["/clock"](SimpleNamespace(clock=SimpleNamespace(sec=1, nanosec=0)))
    node.callbacks["/clock"](SimpleNamespace(clock=SimpleNamespace(sec=2, nanosec=0)))
    with pytest.raises(RosObserverError, match="exceeded the configured limit"):
        observer.stop_clock_observation()
    observer.close()


def test_ros_observer_keeps_failed_shutdown_terminal_but_retryable() -> None:
    node = FakeNode()
    node.shutdown_results = [False, True]
    observer = RosGraphObserver(
        expected_graph(),
        observe_clock=True,
        module_loader=fake_modules(node).__getitem__,
    )

    assert node.executor_started.wait(timeout=1.0)
    with pytest.raises(RosObserverError, match="did not stop"):
        observer.close()
    with pytest.raises(RosObserverError, match="closing"):
        observer.snapshot()
    assert not node.destroyed
    observer.close()
    assert node.destroyed


def test_ros_observer_context_manager_detaches() -> None:
    node = FakeNode()
    modules = fake_modules(node)

    with RosGraphObserver(
        expected_graph(),
        observe_clock=False,
        module_loader=modules.__getitem__,
    ) as observer:
        observer.snapshot()

    assert node.destroyed


def test_ros_observer_skips_action_graph_outside_scenario_scope() -> None:
    def unexpected_query(*_args: object) -> None:
        pytest.fail("unexpected action graph query")

    node = FakeNode()
    modules = fake_modules(node)
    modules["rclpy.action"] = SimpleNamespace(
        get_action_server_names_and_types_by_node=unexpected_query,
        get_action_client_names_and_types_by_node=unexpected_query,
    )
    graph = {"topics": [], "services": [], "actions": [], "lifecycle_nodes": []}

    with RosGraphObserver(
        graph,
        observe_clock=False,
        module_loader=modules.__getitem__,
    ) as observer:
        assert observer.snapshot().actions == {}


def test_ros_observer_queries_forbidden_names_without_subscribing() -> None:
    node = FakeNode()
    modules = fake_modules(node)
    observer = RosGraphObserver(
        expected_graph(),
        forbidden_graph={
            "topics": ["/cmd_vel"],
            "services": ["/arm", "/camera/get_state"],
            "actions": ["/land"],
        },
        observe_clock=False,
        module_loader=modules.__getitem__,
    )

    snapshot = observer.snapshot()

    assert snapshot.topics["/cmd_vel"].publishers == 1
    assert snapshot.services["/arm"].server_nodes == 1
    assert snapshot.services["/arm"].client_nodes == 0
    assert snapshot.services["/camera/get_state"].client_nodes == 0
    assert snapshot.actions["/land"].server_nodes == 0
    assert snapshot.actions["/land"].client_nodes == 1
    assert "/cmd_vel" not in node.callbacks
    observer.close()

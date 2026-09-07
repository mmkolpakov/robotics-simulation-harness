from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from threading import Lock, Thread
from time import monotonic_ns
from typing import Any, Literal

from robotics_acceptance_harness.readiness import (
    EndpointObservation,
    GraphSnapshot,
    LifecycleObservation,
    TopicObservation,
)
from robotics_acceptance_harness.timing import ClockSample


class RosObserverError(RuntimeError):
    """Raised when the read-only ROS observer cannot be initialized or queried."""


@dataclass(slots=True)
class _LifecycleTracker:
    client: Any
    request_type: Any
    future: Any = None
    observation: LifecycleObservation | None = None


class RosGraphObserver:
    """Read-only rclpy observer attached to an already running ROS domain."""

    DEFAULT_MAX_CLOCK_SAMPLES = 1_000_000

    def __init__(
        self,
        expected_graph: Mapping[str, Any],
        *,
        observe_clock: bool,
        forbidden_graph: Mapping[str, Any] | None = None,
        node_name: str = "robotics_acceptance_observer",
        module_loader: Callable[[str], Any] = import_module,
        max_clock_samples: int = DEFAULT_MAX_CLOCK_SAMPLES,
    ) -> None:
        if max_clock_samples < 1:
            raise ValueError("max_clock_samples must be positive")
        try:
            self._rclpy = module_loader("rclpy")
            actions = module_loader("rclpy.action")
            context_module = module_loader("rclpy.context")
            executor_module = module_loader("rclpy.executors")
            self._qos = module_loader("rclpy.qos")
            utilities = module_loader("rosidl_runtime_py.utilities")
            lifecycle_services = module_loader("lifecycle_msgs.srv")
            clock_messages = module_loader("rosgraph_msgs.msg")
        except ModuleNotFoundError as error:
            raise RosObserverError(
                "rclpy, rosidl_runtime_py, lifecycle_msgs, and rosgraph_msgs "
                "must be available in the ROS runtime"
            ) from error

        self._expected_graph = expected_graph
        self._forbidden_graph = forbidden_graph or {
            "topics": (),
            "services": (),
            "actions": (),
        }
        self._context = context_module.Context()
        self._rclpy.init(args=None, context=self._context)
        try:
            self._node = self._rclpy.create_node(
                node_name,
                context=self._context,
                enable_rosout=False,
                start_parameter_services=False,
            )
            self._executor = executor_module.SingleThreadedExecutor(context=self._context)
            self._executor.add_node(self._node)
            self._observation_lock = Lock()
            self._subscriptions: list[Any] = []
            self._own_subscription_counts: dict[str, int] = {}
            self._first_messages: dict[str, int] = {}
            self._topic_qos: dict[str, Any] = {}
            self._clock_samples: list[ClockSample] = []
            self._max_clock_samples = max_clock_samples
            self._clock_sample_overflow = False
            self._record_clock = False
            self._spin_error: Exception | None = None
            self._state: Literal["open", "closing", "closed"] = "open"
            self._get_message = utilities.get_message
            self._get_state_type = lifecycle_services.GetState
            self._clock_type = clock_messages.Clock
            self._get_action_server_names_and_types_by_node = (
                actions.get_action_server_names_and_types_by_node
            )
            self._get_action_client_names_and_types_by_node = (
                actions.get_action_client_names_and_types_by_node
            )
            self._lifecycle: dict[str, _LifecycleTracker] = {}
            self._configure_topics(observe_clock)
            self._configure_lifecycle()
            self._spin_thread = Thread(
                target=self._spin,
                name="robotics-acceptance-ros-observer",
                daemon=True,
            )
            self._spin_thread.start()
        except Exception:
            self._context.try_shutdown()
            raise

    @property
    def clock_samples(self) -> tuple[ClockSample, ...]:
        with self._observation_lock:
            return tuple(self._clock_samples)

    def start_clock_observation(self) -> None:
        self._require_running()
        with self._observation_lock:
            self._clock_samples.clear()
            self._clock_sample_overflow = False
            self._record_clock = True

    def stop_clock_observation(self) -> tuple[ClockSample, ...]:
        self._require_running()
        with self._observation_lock:
            self._record_clock = False
            overflow = self._clock_sample_overflow
            samples = tuple(self._clock_samples)
        if overflow:
            raise RosObserverError(
                "clock observation exceeded the configured limit of "
                f"{self._max_clock_samples} unique samples"
            )
        return samples

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception as error:
            with self._observation_lock:
                self._spin_error = error

    def _require_running(self) -> None:
        if self._state != "open":
            raise RosObserverError(f"observer is {self._state}")
        with self._observation_lock:
            error = self._spin_error
        if error is not None:
            raise RosObserverError("ROS executor stopped unexpectedly") from error
        if not self._spin_thread.is_alive():
            raise RosObserverError("ROS executor stopped unexpectedly")

    def _qos_profile(self, name: str) -> Any:
        profiles = {
            "system_default": self._qos.qos_profile_system_default,
            "sensor_data": self._qos.qos_profile_sensor_data,
            "services_default": self._qos.qos_profile_services_default,
            "parameters": self._qos.qos_profile_parameters,
        }
        return profiles[name]

    def _message_callback(self, topic: str) -> Callable[[Any], None]:
        def callback(_message: Any) -> None:
            with self._observation_lock:
                self._first_messages.setdefault(topic, monotonic_ns())

        return callback

    def _clock_callback(self, message: Any) -> None:
        observed_at_ns = monotonic_ns()
        source_time_ns = int(message.clock.sec) * 1_000_000_000 + int(message.clock.nanosec)
        with self._observation_lock:
            self._first_messages.setdefault("/clock", observed_at_ns)
            changed = (
                not self._clock_samples or self._clock_samples[-1].source_time_ns != source_time_ns
            )
            if self._record_clock and changed:
                if len(self._clock_samples) >= self._max_clock_samples:
                    self._clock_sample_overflow = True
                    return
                self._clock_samples.append(ClockSample(observed_at_ns, source_time_ns))

    def _subscribe(self, topic: str, message_type: Any, qos_profile: Any) -> None:
        callback = self._clock_callback if topic == "/clock" else self._message_callback(topic)
        subscription = self._node.create_subscription(
            message_type,
            topic,
            callback,
            qos_profile,
        )
        self._subscriptions.append(subscription)
        self._own_subscription_counts[topic] = self._own_subscription_counts.get(topic, 0) + 1
        self._topic_qos[topic] = qos_profile

    def _configure_topics(self, observe_clock: bool) -> None:
        for expected in self._expected_graph["topics"]:
            topic = str(expected["name"])
            message_type = self._get_message(str(expected["type"]))
            profile_name = str(expected.get("qos_profile", "system_default"))
            self._subscribe(topic, message_type, self._qos_profile(profile_name))
        if observe_clock and "/clock" not in self._own_subscription_counts:
            clock_qos = self._qos.QoSProfile(
                depth=1,
                reliability=self._qos.ReliabilityPolicy.BEST_EFFORT,
                durability=self._qos.DurabilityPolicy.VOLATILE,
            )
            self._subscribe("/clock", self._clock_type, clock_qos)

    def _configure_lifecycle(self) -> None:
        for expected in self._expected_graph["lifecycle_nodes"]:
            name = str(expected["name"])
            service_name = f"{name.rstrip('/')}/get_state"
            client = self._node.create_client(self._get_state_type, service_name)
            self._lifecycle[name] = _LifecycleTracker(client, self._get_state_type.Request)

    def _observed_names(self, kind: str) -> tuple[str, ...]:
        expected = (str(item["name"]) for item in self._expected_graph[kind])
        forbidden = (str(name) for name in self._forbidden_graph[kind])
        return tuple(dict.fromkeys((*expected, *forbidden)))

    def _poll_lifecycle(self, observed_at_ns: int) -> None:
        for tracker in self._lifecycle.values():
            if tracker.future is not None and tracker.future.done():
                try:
                    response = tracker.future.result()
                    tracker.observation = LifecycleObservation(
                        state=str(response.current_state.label).lower(),
                        observed_at_ns=observed_at_ns,
                    )
                except Exception:
                    tracker.observation = None
                tracker.future = None
            if tracker.future is None and tracker.client.service_is_ready():
                tracker.future = tracker.client.call_async(tracker.request_type())

    def _external_nodes(self) -> tuple[tuple[str, str], ...]:
        own = (self._node.get_name(), self._node.get_namespace())
        return tuple(item for item in self._node.get_node_names_and_namespaces() if item != own)

    def _action_observations(self) -> dict[str, EndpointObservation]:
        observed_names = self._observed_names("actions")
        if not observed_names:
            return {}
        types: dict[str, set[str]] = {}
        server_nodes: dict[str, int] = {}
        client_nodes: dict[str, int] = {}
        for node_name, node_namespace in self._external_nodes():
            for name, action_types in self._get_action_server_names_and_types_by_node(
                self._node,
                node_name,
                node_namespace,
            ):
                types.setdefault(name, set()).update(action_types)
                server_nodes[name] = server_nodes.get(name, 0) + 1
            for name, action_types in self._get_action_client_names_and_types_by_node(
                self._node,
                node_name,
                node_namespace,
            ):
                types.setdefault(name, set()).update(action_types)
                client_nodes[name] = client_nodes.get(name, 0) + 1
        return {
            name: EndpointObservation(
                types=tuple(sorted(types.get(name, ()))),
                server_nodes=server_nodes.get(name, 0),
                client_nodes=client_nodes.get(name, 0),
            )
            for name in observed_names
        }

    def _service_observations(self) -> dict[str, EndpointObservation]:
        observed_names = self._observed_names("services")
        if not observed_names:
            return {}
        types: dict[str, set[str]] = {}
        server_nodes: dict[str, int] = {}
        client_nodes: dict[str, int] = {}
        for node_name, node_namespace in self._external_nodes():
            for name, server_types in self._node.get_service_names_and_types_by_node(
                node_name,
                node_namespace,
            ):
                types.setdefault(name, set()).update(server_types)
                server_nodes[name] = server_nodes.get(name, 0) + 1
            for name, client_types in self._node.get_client_names_and_types_by_node(
                node_name,
                node_namespace,
            ):
                types.setdefault(name, set()).update(client_types)
                client_nodes[name] = client_nodes.get(name, 0) + 1
        return {
            name: EndpointObservation(
                types=tuple(sorted(types.get(name, ()))),
                server_nodes=server_nodes.get(name, 0),
                client_nodes=client_nodes.get(name, 0),
            )
            for name in observed_names
        }

    def _qos_compatible(self, topic: str) -> bool:
        profile = self._topic_qos[topic]
        publisher_info = self._node.get_publishers_info_by_topic(topic)
        error = self._qos.QoSCompatibility.ERROR
        return all(
            self._qos.qos_check_compatible(info.qos_profile, profile)[0] != error
            for info in publisher_info
        )

    def snapshot(self) -> GraphSnapshot:
        self._require_running()
        observed_at_ns = monotonic_ns()
        self._poll_lifecycle(observed_at_ns)
        with self._observation_lock:
            first_messages = dict(self._first_messages)

        topic_types = dict(self._node.get_topic_names_and_types())
        topics: dict[str, TopicObservation] = {}
        for name in self._observed_names("topics"):
            subscribers = max(
                0,
                self._node.count_subscribers(name) - self._own_subscription_counts.get(name, 0),
            )
            topics[name] = TopicObservation(
                types=tuple(topic_types.get(name, ())),
                publishers=self._node.count_publishers(name),
                subscribers=subscribers,
                first_message_at_ns=first_messages.get(name),
                qos_compatible=(self._qos_compatible(name) if name in self._topic_qos else True),
            )

        services = self._service_observations()
        actions = self._action_observations()
        lifecycle = {
            name: tracker.observation
            for name, tracker in self._lifecycle.items()
            if tracker.observation is not None
        }
        return GraphSnapshot(
            observed_at_ns=observed_at_ns,
            topics=topics,
            services=services,
            actions=actions,
            lifecycle_nodes=lifecycle,
        )

    def close(self) -> None:
        if self._state == "closed":
            return
        self._state = "closing"
        stopped = self._executor.shutdown(timeout_sec=5.0)
        if not stopped:
            raise RosObserverError("ROS executor did not stop before the timeout")
        self._spin_thread.join(timeout=5.0)
        if self._spin_thread.is_alive():
            raise RosObserverError("ROS executor did not stop before the timeout")
        self._executor.remove_node(self._node)
        self._node.destroy_node()
        self._context.try_shutdown()
        self._state = "closed"

    def __enter__(self) -> RosGraphObserver:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

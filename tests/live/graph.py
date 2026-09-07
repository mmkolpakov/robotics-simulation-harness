from __future__ import annotations

from threading import Thread
from time import time_ns
from uuid import uuid4

import rclpy
from action_tutorials_interfaces.action import Fibonacci
from example_interfaces.srv import AddTwoInts
from rclpy.action import ActionClient, ActionServer
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import serialize_message
from rosgraph_msgs.msg import Clock
from std_msgs.msg import String


class LiveGraph:
    """Test-owned ROS nodes; the harness only observes their real DDS endpoints."""

    def __init__(self) -> None:
        self.namespace = f"/harness_live_{uuid4().hex[:8]}"
        self.topic = f"{self.namespace}/status"
        self.service = f"{self.namespace}/add_two_ints"
        self.action = f"{self.namespace}/fibonacci"
        self.lifecycle = f"{self.namespace}/managed"
        self.context = Context()
        rclpy.init(context=self.context)
        self.producer = rclpy.create_node(
            "producer", namespace=self.namespace, context=self.context
        )
        self.consumer = rclpy.create_node(
            "consumer", namespace=self.namespace, context=self.context
        )
        self.managed = LifecycleNode("managed", namespace=self.namespace, context=self.context)
        clock_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.clock_publisher = self.producer.create_publisher(Clock, "/clock", clock_qos)
        self.recorded_clock_samples: list[tuple[int, bytes]] = []
        self.clock_subscription = self.consumer.create_subscription(
            Clock, "/clock", self.record_clock, clock_qos
        )
        self.publisher = self.producer.create_publisher(String, self.topic, 10)
        self.subscription = self.consumer.create_subscription(
            String, self.topic, lambda msg: None, 10
        )
        self.server = self.producer.create_service(AddTwoInts, self.service, self.add)
        self.client = self.consumer.create_client(AddTwoInts, self.service)
        self.action_server = ActionServer(
            self.producer, Fibonacci, self.action, execute_callback=self.fibonacci
        )
        self.action_client = ActionClient(self.consumer, Fibonacci, self.action)
        assert self.managed.trigger_configure() == TransitionCallbackReturn.SUCCESS
        assert self.managed.trigger_activate() == TransitionCallbackReturn.SUCCESS
        self.tick = 0
        self.timer = self.producer.create_timer(0.02, self.publish)
        self.executor = SingleThreadedExecutor(context=self.context)
        for node in (self.producer, self.consumer, self.managed):
            self.executor.add_node(node)
        self.errors: list[Exception] = []
        self.thread = Thread(target=self.spin, name="live-ros-fixture", daemon=True)
        self.thread.start()

    @staticmethod
    def add(request, response):
        response.sum = request.a + request.b
        return response

    @staticmethod
    def fibonacci(goal_handle):
        sequence = [0, 1]
        for _ in range(max(0, goal_handle.request.order - 2)):
            sequence.append(sequence[-1] + sequence[-2])
        goal_handle.succeed()
        return Fibonacci.Result(sequence=sequence)

    def publish(self) -> None:
        self.tick += 1
        message = Clock()
        message.clock.sec, message.clock.nanosec = divmod(self.tick * 20_000_000, 1_000_000_000)
        self.clock_publisher.publish(message)
        self.publisher.publish(String(data=str(self.tick)))

    def record_clock(self, message: Clock) -> None:
        self.recorded_clock_samples.append((time_ns(), serialize_message(message)))

    def spin(self) -> None:
        try:
            self.executor.spin()
        except Exception as error:
            self.errors.append(error)

    def expected(self) -> dict:
        # /clock is deliberately absent: this exercises the implicit clock subscription.
        return {
            "topics": [
                {
                    "name": self.topic,
                    "type": "std_msgs/msg/String",
                    "min_publishers": 1,
                    "min_subscribers": 1,
                    "first_message_timeout_sec": 15,
                }
            ],
            "services": [
                {
                    "name": self.service,
                    "type": "example_interfaces/srv/AddTwoInts",
                    "server_required": True,
                }
            ],
            "actions": [
                {
                    "name": self.action,
                    "type": "action_tutorials_interfaces/action/Fibonacci",
                    "server_required": True,
                }
            ],
            "lifecycle_nodes": [
                {
                    "name": self.lifecycle,
                    "required_state": "active",
                    "timeout_sec": 15,
                    "stable_for_sec": 0,
                }
            ],
        }

    def __enter__(self) -> LiveGraph:
        return self

    def __exit__(self, *_args) -> None:
        try:
            assert self.executor.shutdown(timeout_sec=5), "test ROS executor did not stop"
            self.thread.join(timeout=5)
            assert not self.thread.is_alive(), "test ROS spin thread did not stop"
            self.action_client.destroy()
            self.action_server.destroy()
            for node in (self.managed, self.consumer, self.producer):
                self.executor.remove_node(node)
                node.destroy_node()
        finally:
            self.context.try_shutdown()
        assert not self.errors, f"test ROS executor failed: {self.errors}"

from __future__ import annotations

from time import monotonic, sleep

import pytest

from robotics_acceptance_harness.readiness import evaluate_graph, wait_for_readiness
from robotics_acceptance_harness.ros import RosGraphObserver

pytestmark = pytest.mark.live_ros


def test_clock_not_declared_in_expected_graph(live_graph) -> None:
    graph = {"topics": [], "services": [], "actions": [], "lifecycle_nodes": []}
    with RosGraphObserver(graph, observe_clock=True) as observer:
        assert observer.clock_samples == ()
        observer.start_clock_observation()
        deadline = monotonic() + 15
        while len(observer.clock_samples) < 10 and monotonic() < deadline:
            observer.snapshot()
            sleep(0.02)
        samples = observer.stop_clock_observation()
        assert len(samples) >= 10, "no real /clock messages received through implicit subscription"
        assert all(
            b.source_time_ns > a.source_time_ns for a, b in zip(samples, samples[1:], strict=False)
        )
        assert all(
            b.observed_at_ns > a.observed_at_ns for a, b in zip(samples, samples[1:], strict=False)
        )
        sleep(0.1)
        assert observer.clock_samples == samples
        assert observer.snapshot().topics == {}


def test_observes_topic_service_action_and_lifecycle(live_graph) -> None:
    graph = live_graph.expected()
    with RosGraphObserver(graph, observe_clock=False) as observer:
        readiness = wait_for_readiness(
            graph, observer, timeout_sec=20, stable_for_sec=0.3, poll_interval_sec=0.05
        )
        snapshot = readiness.snapshot
        assert evaluate_graph(graph, snapshot) == ()
        topic = snapshot.topics[live_graph.topic]
        assert topic.types == ("std_msgs/msg/String",)
        assert topic.publishers == 1
        assert topic.subscribers == 1  # The observer's subscription must not count.
        assert topic.first_message_at_ns is not None
        assert topic.qos_compatible
        # Discovery of client endpoints can lag behind server readiness.
        deadline = monotonic() + 10
        while monotonic() < deadline:
            snapshot = observer.snapshot()
            if (
                snapshot.services[live_graph.service].client_nodes == 1
                and snapshot.actions[live_graph.action].client_nodes == 1
            ):
                break
            sleep(0.05)
        service = snapshot.services[live_graph.service]
        assert service.types == ("example_interfaces/srv/AddTwoInts",)
        assert (service.server_nodes, service.client_nodes) == (1, 1)
        action = snapshot.actions[live_graph.action]
        assert action.types == ("action_tutorials_interfaces/action/Fibonacci",)
        assert (action.server_nodes, action.client_nodes) == (1, 1)
        assert snapshot.lifecycle_nodes[live_graph.lifecycle].state == "active"
        assert observer.clock_samples == ()

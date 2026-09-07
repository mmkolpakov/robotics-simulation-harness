#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
set -u

mkdir -p "${ROBOTICS_LIVE_ARTIFACTS}"
python -c 'import rclpy; import rclpy.lifecycle; import action_tutorials_interfaces.action'
exec python -m pytest tests/live -m live_ros -ra \
  --junitxml="${ROBOTICS_LIVE_ARTIFACTS}/junit.xml" "$@"

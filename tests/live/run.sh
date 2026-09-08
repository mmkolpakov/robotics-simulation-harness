#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
set -u

# Keep ROS Python bindings visible without autoloading apt-installed pytest plugins.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

mkdir -p "${ROBOTICS_LIVE_ARTIFACTS}"
python -c 'import rclpy; import rclpy.lifecycle; import action_tutorials_interfaces.action'
exec python -m pytest tests/live -m live_ros -ra \
  --junitxml="${ROBOTICS_LIVE_ARTIFACTS}/junit.xml" "$@"

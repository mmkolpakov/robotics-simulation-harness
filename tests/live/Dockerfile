FROM otel/opentelemetry-collector-contrib:0.148.0 AS collector
FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

# Docker Hub has no osrf/ros:jazzy-ros-base tag; ROS base is an official image.
FROM ros:jazzy-ros-base@sha256:2589a8fba5257307857890173c069852c2abf913a0be7970f172478baecb09e4

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends \
        git python3-venv ros-jazzy-rclpy ros-jazzy-rosidl-runtime-py \
        ros-jazzy-rmw-fastrtps-cpp ros-jazzy-rosgraph-msgs ros-jazzy-lifecycle-msgs \
        ros-jazzy-std-msgs ros-jazzy-example-interfaces ros-jazzy-action-tutorials-interfaces \
    && rm -rf /var/lib/apt/lists/*
COPY --from=collector /otelcol-contrib /usr/local/bin/otelcol-contrib
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /harness
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY tests ./tests
# Jazzy's apt-installed rclpy is built for Ubuntu's /usr/bin/python3 (3.12).
# Use a new venv, never an uv-managed Python or the host's .venv.
RUN /usr/bin/python3 -m venv --system-site-packages /opt/live-venv \
    && uv export --locked --all-groups --no-emit-project \
        --format requirements.txt --output-file /tmp/live-requirements.txt \
    && uv pip install --python /opt/live-venv/bin/python \
        --requirement /tmp/live-requirements.txt \
    && uv pip install --python /opt/live-venv/bin/python mcap==1.3.0 \
    && uv pip install --python /opt/live-venv/bin/python --no-deps .

ENV PATH="/opt/live-venv/bin:${PATH}" \
    ROBOTICS_LIVE_ROS=1 \
    ROS_DOMAIN_ID=121 \
    ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
    RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    ROBOTICS_LIVE_ARTIFACTS=/harness/artifacts/live
ENTRYPOINT ["/bin/bash", "/harness/tests/live/run.sh"]

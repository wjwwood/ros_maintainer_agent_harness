# Copyright 2026 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_DISTRO_IMAGES = {
    'rolling': 'docker.io/osrf/ros:rolling-desktop',
    'jazzy': 'docker.io/osrf/ros:jazzy-desktop',
    'iron': 'docker.io/osrf/ros:iron-desktop',
    'humble': 'docker.io/osrf/ros:humble-desktop',
    'kilted': 'docker.io/osrf/ros:kilted-desktop',
    'lyrical': 'docker.io/osrf/ros:lyrical-desktop',
    'noetic': 'docker.io/osrf/ros:noetic-desktop',
}


def get_image_for_distro(distro: str, custom_image: Optional[str] = None) -> str:
    """Resolve the container image name for a target ROS distro."""
    if custom_image:
        return custom_image
    return DEFAULT_DISTRO_IMAGES.get(distro.lower(), f"docker.io/osrf/ros:{distro.lower()}-desktop")


def generate_devcontainer_config(
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a complete devcontainer.json configuration dictionary for a session.
    """
    session_dir = session_dir.resolve()
    workspace_root = workspace_root.resolve()
    tools_dir = workspace_root / 'tools'
    rules_file = workspace_root / 'config' / 'maintainer_rules.md'
    image = get_image_for_distro(distro, custom_image)

    config = {
        "name": f"ROS 2 Maintainer Sandbox ({session_dir.name})",
        "image": image,
        "workspaceFolder": "/workspace",
        "workspaceMount": f"source={session_dir},target=/workspace,type=bind",
        "mounts": [
            f"source={tools_dir},target=/workspace/tools,type=bind",
        ],
        "containerEnv": {
            "PATH": "/workspace/tools/bin:/root/.local/bin:${containerEnv:PATH}",
            "ROS_DISTRO": distro,
            "ROS_MAINTAINER_SESSION_ID": session_dir.name,
            "ROS_MAINTAINER_GATEWAY_URL": gateway_url or "http://host.docker.internal:8765",
            "PYTHONUNBUFFERED": "1",
        },
        "runArgs": [
            "--add-host=host.docker.internal:host-gateway",
            "--security-opt=seccomp=unconfined",
        ],
        "customizations": {
            "vscode": {
                "extensions": [
                    "ms-vscode.cpptools",
                    "ms-python.python",
                    "ms-iot.vscode-ros",
                    "twxs.cmake",
                    "eamodio.gitlens",
                ],
                "settings": {
                    "terminal.integrated.defaultProfile.linux": "bash",
                    "python.defaultInterpreterPath": "/usr/bin/python3",
                },
            },
        },
        "postCreateCommand": (
            "bash -c 'source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null && "
            "echo \"source /opt/ros/$ROS_DISTRO/setup.bash\" >> ~/.bashrc && "
            "(pip install -r /workspace/tools/requirements.txt 2>/dev/null || true)'"
        ),
    }

    if rules_file.exists():
        config["mounts"].append(f"source={rules_file},target=/workspace/MAINTAINER_RULES.md,type=bind,readonly")

    return config


def write_devcontainer_config(
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> Path:
    """
    Write the devcontainer.json configuration to the session's .devcontainer directory.
    """
    devcontainer_dir = session_dir / '.devcontainer'
    devcontainer_dir.mkdir(parents=True, exist_ok=True)
    config_file = devcontainer_dir / 'devcontainer.json'

    config = generate_devcontainer_config(
        session_dir=session_dir,
        workspace_root=workspace_root,
        distro=distro,
        custom_image=custom_image,
        gateway_url=gateway_url,
    )

    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

    return config_file

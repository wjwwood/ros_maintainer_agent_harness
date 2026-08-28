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
import tempfile
import unittest

from ros_maintainer_agent_harness.devcontainer import (
    DEFAULT_DISTRO_IMAGES,
    generate_devcontainer_config,
    get_image_for_distro,
    write_devcontainer_config,
)
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestDevcontainer(unittest.TestCase):

    def test_get_image_for_distro(self):
        self.assertEqual(get_image_for_distro('rolling'), DEFAULT_DISTRO_IMAGES['rolling'])
        self.assertEqual(get_image_for_distro('jazzy'), DEFAULT_DISTRO_IMAGES['jazzy'])
        self.assertEqual(get_image_for_distro('humble'), DEFAULT_DISTRO_IMAGES['humble'])
        self.assertEqual(
            get_image_for_distro('custom_distro', custom_image='myorg/ros:custom'),
            'myorg/ros:custom',
        )

    def test_generate_devcontainer_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            session_dir = ws_root / 'sessions' / 'session-pr-160'
            session_dir.mkdir(parents=True, exist_ok=True)

            config = generate_devcontainer_config(
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='jazzy',
            )

            self.assertIn('ROS 2 Maintainer Sandbox (session-pr-160)', config['name'])
            self.assertEqual(config['image'], DEFAULT_DISTRO_IMAGES['jazzy'])
            self.assertEqual(config['workspaceFolder'], '/workspace')
            self.assertIn(str(session_dir), config['workspaceMount'])
            self.assertEqual(config['containerEnv']['ROS_DISTRO'], 'jazzy')
            self.assertEqual(config['containerEnv']['ROS_MAINTAINER_SESSION_ID'], 'session-pr-160')
            self.assertIn('--add-host=host.docker.internal:host-gateway', config['runArgs'])

            # Verify mount of maintainer_rules.md
            mounts = config['mounts']
            rules_mount = any('MAINTAINER_RULES.md' in m for m in mounts)
            self.assertTrue(rules_mount)

    def test_write_devcontainer_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            session_dir = ws_root / 'sessions' / 'session-pr-99'
            session_dir.mkdir(parents=True, exist_ok=True)

            out_file = write_devcontainer_config(
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='rolling',
                gateway_url='http://192.168.1.5:8765',
            )

            self.assertTrue(out_file.exists())
            self.assertEqual(out_file.name, 'devcontainer.json')

            with open(out_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.assertEqual(data['containerEnv']['ROS_MAINTAINER_GATEWAY_URL'], 'http://192.168.1.5:8765')
            self.assertEqual(data['containerEnv']['ROS_DISTRO'], 'rolling')

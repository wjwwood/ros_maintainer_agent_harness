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

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestTools(unittest.TestCase):

    def test_default_tools_populated_and_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            expected_scripts = [
                'ros-find-restarted-ci',
                'ros-ci-for-pr',
                'ros-session-status',
            ]

            for script_name in expected_scripts:
                script_path = layout.tools_bin_dir / script_name
                self.assertTrue(script_path.exists(), f"Missing script: {script_name}")
                if os.name != 'nt':
                    self.assertTrue(os.access(script_path, os.X_OK), f"Not executable: {script_name}")

                # Test running each script with --help
                res = subprocess.run(
                    [sys.executable, str(script_path), '--help'],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res.returncode, 0, f"Error running {script_name} --help: {res.stderr}")
                self.assertIn('usage:', res.stdout.lower())

    def test_ros_session_status_logging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            timeline_path = ws_root / 'timeline.md'
            timeline_path.write_text("# Timeline\n", encoding='utf-8')

            status_script = layout.tools_bin_dir / 'ros-session-status'

            # Run status message
            res = subprocess.run(
                [
                    sys.executable,
                    str(status_script),
                    'Built rclcpp package',
                    '--timeline-file',
                    str(timeline_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)

            # Run milestone message
            res = subprocess.run(
                [
                    sys.executable,
                    str(status_script),
                    'All tests passing',
                    '-m',
                    'Tests Green',
                    '--timeline-file',
                    str(timeline_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)

            content = timeline_path.read_text(encoding='utf-8')
            self.assertIn('Built rclcpp package', content)
            self.assertIn('🏆 **Milestone**: Tests Green — All tests passing', content)

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

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ros_maintainer_agent_harness.pr_harvester import PRMetadata
from ros_maintainer_agent_harness.scaffolder import (
    generate_task_prompt,
    scaffold_session_from_pr,
)
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestScaffolder(unittest.TestCase):

    def setUp(self):
        self.mock_meta = PRMetadata(
            owner='ros2',
            repo='rclcpp',
            number=160,
            title='Fix race condition in timer callback',
            body='This PR resolves a mutex deadlock during shutdown.',
            base_ref='jazzy',
            head_ref='fix-timer-race',
            head_repo_owner='contributor',
            head_repo_url='',
            is_fork=True,
            url='https://github.com/ros2/rclcpp/pull/160',
            detected_distro='jazzy',
            changed_files=['src/rclcpp/timer.cpp', 'test/test_timer.cpp'],
            labels=['bug', 'jazzy'],
            author='contributor',
        )

    def test_generate_task_prompt(self):
        prompt = generate_task_prompt(self.mock_meta, distro='jazzy')
        self.assertIn('PR #160', prompt)
        self.assertIn('ros2/rclcpp', prompt)
        self.assertIn('src/rclcpp/timer.cpp', prompt)
        self.assertIn('jazzy', prompt)
        self.assertIn('colcon test', prompt)

    @patch('ros_maintainer_agent_harness.scaffolder.fetch_pr_metadata')
    def test_scaffold_session_from_pr(self, mock_fetch):
        mock_fetch.return_value = self.mock_meta

        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            # Set up mock git repository in shared_repos/rclcpp
            repo_dir = layout.shared_repos_dir / 'rclcpp'
            repo_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'init'], cwd=str(repo_dir), capture_output=True, check=True)
            subprocess.run(
                ['git', 'config', 'user.name', 'Tester'], cwd=str(repo_dir), capture_output=True, check=True
            )
            subprocess.run(
                ['git', 'config', 'user.email', 'tester@example.com'],
                cwd=str(repo_dir), capture_output=True, check=True,
            )
            (repo_dir / 'README.md').write_text('# rclcpp\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'README.md'], cwd=str(repo_dir), capture_output=True, check=True)
            subprocess.run(
                ['git', 'commit', '-m', 'Initial commit'],
                cwd=str(repo_dir), capture_output=True, check=True,
            )
            subprocess.run(['git', 'branch', '-M', 'jazzy'], cwd=str(repo_dir), capture_output=True, check=True)

            res = scaffold_session_from_pr(
                workspace=layout,
                pr_ref='ros2/rclcpp#160',
            )

            self.assertEqual(res.session_id, 'pr-rclcpp-160')
            self.assertEqual(res.distro, 'jazzy')
            self.assertTrue(res.session_dir.exists())
            self.assertTrue(res.worktree_path.exists())
            self.assertTrue((res.worktree_path / 'README.md').exists())

            # Check devcontainer
            devcontainer_path = res.session_dir / '.devcontainer' / 'devcontainer.json'
            self.assertTrue(devcontainer_path.exists())

            # Check MCP configs
            self.assertTrue((res.session_dir / 'mcp.json').exists())
            self.assertTrue((res.session_dir / '.mcp.json').exists())
            self.assertTrue((res.session_dir / '.cursor' / 'mcp.json').exists())
            self.assertTrue((res.session_dir / '.vscode' / 'mcp.json').exists())
            self.assertTrue((res.session_dir / '.gemini' / 'mcp_config.json').exists())

            # Check timeline.md and TASK.md
            self.assertTrue(res.timeline_path.exists())
            self.assertIn('PR #160', res.timeline_path.read_text(encoding='utf-8'))
            self.assertTrue(res.task_file.exists())
            self.assertIn('Fix race condition in timer callback', res.task_file.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

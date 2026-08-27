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

from ros_maintainer_agent_harness.git_ops import (
    execute_git_push,
    extract_repo_full_name,
    get_current_branch,
    get_current_commit_sha,
    get_repo_remote_url,
)


class TestGitOps(unittest.TestCase):

    def test_extract_repo_full_name(self):
        self.assertEqual(
            extract_repo_full_name('https://github.com/ros2/rclcpp.git'),
            'ros2/rclcpp',
        )
        self.assertEqual(
            extract_repo_full_name('http://github.com/ros2/rclcpp'),
            'ros2/rclcpp',
        )
        self.assertEqual(
            extract_repo_full_name('git@github.com:ros2/rclcpp.git'),
            'ros2/rclcpp',
        )
        self.assertEqual(
            extract_repo_full_name('ssh://git@github.com/ros2/rclcpp.git'),
            'ros2/rclcpp',
        )
        self.assertEqual(
            extract_repo_full_name('ssh://git@github.com:22/ros2/rclcpp.git'),
            'ros2/rclcpp',
        )
        self.assertEqual(
            extract_repo_full_name('git://github.com/ros2/rclcpp.git'),
            'ros2/rclcpp',
        )
        self.assertEqual(
            extract_repo_full_name('https://github.com/wjwwood/rosidl_typesupport_fastrtps'),
            'wjwwood/rosidl_typesupport_fastrtps',
        )
        self.assertIsNone(extract_repo_full_name(''))

    def test_git_push_operations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / 'test_repo'
            repo_path.mkdir()

            # Initialize dummy git repo
            subprocess.run(['git', 'init', '-b', 'wjwwood/fix'], cwd=str(repo_path), check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(repo_path), check=True)
            subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=str(repo_path), check=True)

            (repo_path / 'README.md').write_text('hello')
            subprocess.run(['git', 'add', '.'], cwd=str(repo_path), check=True)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(repo_path), check=True)

            # Test branch detection
            self.assertEqual(get_current_branch(repo_path), 'wjwwood/fix')
            sha = get_current_commit_sha(repo_path)
            self.assertIsNotNone(sha)
            self.assertEqual(len(sha), 40)

            # Add dummy remote
            remote_repo = Path(temp_dir) / 'remote_repo.git'
            subprocess.run(['git', 'init', '--bare', str(remote_repo)], check=True)
            subprocess.run(
                ['git', 'remote', 'add', 'origin', str(remote_repo)], cwd=str(repo_path), check=True
            )

            # Check remote URL
            self.assertEqual(get_repo_remote_url(repo_path, 'origin'), str(remote_repo))

            # Push to remote
            success, msg = execute_git_push(
                repo_dir=repo_path,
                branch='wjwwood/fix',
                remote='origin',
                force_with_lease=True,
                dry_run=False,
            )
            self.assertTrue(success)


if __name__ == '__main__':
    unittest.main()

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
from unittest.mock import MagicMock, patch

from ros_maintainer_agent_harness.git_ops import (
    execute_git_push,
    execute_release_push,
    extract_repo_full_name,
    get_current_branch,
    get_current_commit_sha,
    get_repo_remote_url,
    git_safe_cmd,
    is_remote_tracking_writable,
    strip_harmless_remote_ref_lock_errors,
)


class TestGitOps(unittest.TestCase):

    def test_git_safe_cmd_passes_safe_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / 'container_cloned_repo'
            repo_path.mkdir()
            cmd = git_safe_cmd(repo_path, 'remote', 'get-url', '--', 'origin')
            self.assertEqual(
                cmd,
                ['git', '-c', f'safe.directory={repo_path.resolve()}', 'remote', 'get-url', '--', 'origin'],
            )

            with patch('ros_maintainer_agent_harness.git_ops.subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout='https://github.com/ros2/ros2_documentation.git\n',
                    stderr='',
                )
                url = get_repo_remote_url(repo_path, 'origin')
                self.assertEqual(url, 'https://github.com/ros2/ros2_documentation.git')
                called_cmd = mock_run.call_args[0][0]
                self.assertIn(f'safe.directory={repo_path.resolve()}', called_cmd)

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

            # Simulate container root-owned .git/refs/remotes/origin (read-only to host user)
            remotes_dir = repo_path / '.git' / 'refs' / 'remotes' / 'origin'
            remotes_dir.mkdir(parents=True, exist_ok=True)
            ro_ref = remotes_dir / 'wjwwood_readonly_ref'
            ro_ref.write_text(f"{sha}\n")
            ro_ref.chmod(0o444)
            remotes_dir.chmod(0o555)
            try:
                self.assertFalse(is_remote_tracking_writable(repo_path, 'origin'))
                (repo_path / 'README.md').write_text('release 0.30.2')
                subprocess.run(['git', 'commit', '-am', '0.30.2'], cwd=str(repo_path), check=True)
                subprocess.run(['git', 'tag', '0.30.2'], cwd=str(repo_path), check=True)

                rel_ok, rel_out = execute_release_push(
                    repo_dir=repo_path,
                    target_branch='rolling',
                    tag='0.30.2',
                    remote='origin',
                )
                self.assertTrue(rel_ok, rel_out)
                self.assertNotIn('update_ref failed', rel_out)
            finally:
                remotes_dir.chmod(0o755)
                ro_ref.chmod(0o644)

            raw_stderr = (
                "To github.com:ros2/launch_ros.git\n"
                "   39f64b4..f53d2fb  f53d2fb -> rolling\n"
                " * [new tag]         0.30.2 -> 0.30.2\n"
                "error: update_ref failed for ref 'refs/remotes/origin/rolling': "
                "cannot lock ref 'refs/remotes/origin/rolling': Unable to create "
                "'/workspace/src/launch_ros/.git/refs/remotes/origin/rolling.lock': Permission denied\n"
            )
            cleaned = strip_harmless_remote_ref_lock_errors(raw_stderr)
            self.assertIn('To github.com:ros2/launch_ros.git', cleaned)
            self.assertNotIn('update_ref failed', cleaned)


if __name__ == '__main__':
    unittest.main()

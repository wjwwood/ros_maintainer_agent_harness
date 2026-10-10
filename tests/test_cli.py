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

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ros_maintainer_agent_harness.approval import ApprovalManager
from ros_maintainer_agent_harness.ci import JenkinsManager
from ros_maintainer_agent_harness.cli import get_default_workspace_path, main
from ros_maintainer_agent_harness.timeline import TimelineLogger
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestCLI(unittest.TestCase):

    def test_cli_init_and_session_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = str(Path(temp_dir) / 'cli_ws')

            # 1. Test init
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'init']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Successfully initialized maintainer workspace', fake_out.getvalue())

            # 2. Test rules add & show
            add_args = [
                'ros-maintainer-harness', '-w', ws_root, 'rules', 'add', 'Testing', 'Run pytest with -v'
            ]
            with patch.object(sys, 'argv', add_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Added rule to category 'Testing'", fake_out.getvalue())

            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'rules', 'show']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Run pytest with -v', fake_out.getvalue())

            # 3. Test session create with distro
            create_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'create', 'session-pr-42',
                '--topic', 'PR 42 Fix', '--distro', 'jazzy'
            ]
            with patch.object(sys, 'argv', create_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Created session 'session-pr-42' (distro: jazzy)", fake_out.getvalue())
                    self.assertIn("Devcontainer:", fake_out.getvalue())

            # 3b. Test session devcontainer generation
            devcontainer_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'devcontainer', 'session-pr-42',
                '--distro', 'humble'
            ]
            with patch.object(sys, 'argv', devcontainer_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Generated .devcontainer configuration", fake_out.getvalue())

            # 4. Test session launch --dry-run
            launch_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'launch', 'session-pr-42',
                '--agent', 'claude', '--dry-run'
            ]
            with patch.object(sys, 'argv', launch_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Launch configuration for session 'session-pr-42'", fake_out.getvalue())
                    self.assertIn("claude --cwd", fake_out.getvalue())

            # 4b. Test session mcp-config
            mcp_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'mcp-config', 'session-pr-42'
            ]
            with patch.object(sys, 'argv', mcp_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Generated MCP client configuration files", fake_out.getvalue())

            # 4c. Test session from-pr (mocked scaffold)
            with patch('ros_maintainer_agent_harness.cli.scaffold_session_from_pr') as mock_scaffold:
                from ros_maintainer_agent_harness.pr_harvester import PRMetadata
                from ros_maintainer_agent_harness.scaffolder import ScaffoldResult
                dummy_meta = PRMetadata(
                    owner='ros2', repo='rclcpp', number=160, title='Fix timer', body='',
                    base_ref='rolling', head_ref='fix', head_repo_owner='ros2',
                    head_repo_url='', is_fork=False, url='https://github.com/ros2/rclcpp/pull/160',
                    detected_distro='rolling', changed_files=['timer.cpp'],
                )
                mock_scaffold.return_value = ScaffoldResult(
                    session_id='pr-rclcpp-160',
                    session_dir=Path(ws_root) / 'sessions' / 'pr-rclcpp-160',
                    pr_metadata=dummy_meta,
                    worktree_path=Path(ws_root) / 'sessions' / 'pr-rclcpp-160' / 'src' / 'rclcpp',
                    distro='rolling',
                    task_file=Path(ws_root) / 'sessions' / 'pr-rclcpp-160' / 'TASK.md',
                    timeline_path=Path(ws_root) / 'sessions' / 'pr-rclcpp-160' / 'timeline.md',
                )
                from_pr_args = ['ros-maintainer-harness', '-w', ws_root, 'session', 'from-pr', 'ros2/rclcpp#160']
                with patch.object(sys, 'argv', from_pr_args):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn("Successfully scaffolded session 'pr-rclcpp-160'", fake_out.getvalue())

            # 5. Test session list
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'session', 'list']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('session-pr-42', fake_out.getvalue())

            # 6. Test session prune
            prune_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'prune', 'session-pr-42'
            ]
            with patch.object(sys, 'argv', prune_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Successfully pruned session 'session-pr-42'", fake_out.getvalue())

    def test_cli_policy_subcommands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = str(Path(temp_dir) / 'cli_ws')

            # Initialize
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'init']):
                with patch('sys.stdout', new=io.StringIO()):
                    main()

            # Policy show
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'policy', 'show']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('allowed_branch_patterns', fake_out.getvalue())

            # Policy check allowed
            check_args = [
                'ros-maintainer-harness', '-w', ws_root, 'policy', 'check',
                '--branch', 'wjwwood/fix_feature', '--repo', 'ros2/rclcpp'
            ]
            with patch.object(sys, 'argv', check_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('POLICY ALLOWED', fake_out.getvalue())

            # Policy check blocked (rolling)
            check_blocked_args = [
                'ros-maintainer-harness', '-w', ws_root, 'policy', 'check',
                '--branch', 'rolling', '--repo', 'ros2/rclcpp'
            ]
            with patch.object(sys, 'argv', check_blocked_args):
                with patch('sys.stderr', new=io.StringIO()) as fake_err:
                    ret = main()
                    self.assertEqual(ret, 1)
                    self.assertIn('POLICY DENIED', fake_err.getvalue())

    def test_cli_audit_subcommand(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'cli_ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            # Create an audit log record
            timeline = TimelineLogger('session-1', ws_root / 'sessions' / 'session-1', layout.audit_log_path)
            timeline.log_action(
                action='git_push',
                target='ros2/rclcpp:fix_branch',
                reason='Pushing fix for PR 160',
                status='APPROVED',
            )

            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws_root), 'audit', 'show']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('git_push', fake_out.getvalue())
                    self.assertIn('Pushing fix for PR 160', fake_out.getvalue())

    def test_cli_approval_subcommands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'cli_ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            approval_mgr = ApprovalManager(layout.approvals_path)
            req = approval_mgr.create_request(
                session_id='session-pr-99',
                action='create_pull_request',
                target='ros2/rclcpp',
                reason='Test PR approval flow',
            )

            # 1. approval list
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws_root), 'approval', 'list']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn(req.ticket_id, fake_out.getvalue())
                    self.assertIn('Test PR approval flow', fake_out.getvalue())

            # 2. approval approve
            appr_args = [
                'ros-maintainer-harness', '-w', str(ws_root), 'approval', 'approve', req.ticket_id,
                '--maintainer', 'wjwwood', '--comment', 'Approved for merge'
            ]
            with patch.object(sys, 'argv', appr_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Approved ticket', fake_out.getvalue())

            # Verify ticket is approved
            self.assertTrue(approval_mgr.is_approved(req.ticket_id))

    def test_cli_ci_subcommands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'cli_ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            from ros_maintainer_agent_harness.ci import CITracker
            tracker = CITracker(layout.ci_runs_path)
            tracker.record_run(
                pr_url='ros2/rclcpp#160',
                session_id='session-1',
                job_name='ci_launcher',
                build_num=555,
                job_url='https://ci.ros2.org/job/ci_launcher/555/',
                status='RUNNING',
            )

            # 1. ci list
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws_root), 'ci', 'list']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('555', fake_out.getvalue())
                    self.assertIn('ros2/rclcpp#160', fake_out.getvalue())

            # 2. ci status (mocking fetch_build_status)
            mock_status = {'success': True, 'status': 'RUNNING', 'duration_seconds': 30.0}
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws_root), 'ci', 'status', '555']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    with patch.object(JenkinsManager, 'fetch_build_status', return_value=mock_status):
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('Build Status: RUNNING', fake_out.getvalue())

            # 3. ci summary (mocking fetch_test_report)
            mock_failed_build = {'success': True, 'status': 'FAILURE', 'duration_seconds': 60.0}
            mock_report = {
                'success': True,
                'total': 50,
                'passed': 49,
                'failed': 1,
                'skipped': 0,
                'failures': [{'name': 'test_deadlock', 'error_details': 'Timeout'}],
            }
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', str(ws_root), 'ci', 'summary', '555']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    with patch.object(JenkinsManager, 'fetch_build_status', return_value=mock_failed_build):
                        with patch.object(JenkinsManager, 'fetch_test_report', return_value=mock_report):
                            with patch.object(JenkinsManager, 'fetch_console_excerpt', return_value='FAILED: dead'):
                                ret = main()
                                self.assertEqual(ret, 0)
                                self.assertIn('CI Summary', fake_out.getvalue())
                                self.assertIn('test_deadlock', fake_out.getvalue())

    def test_get_default_workspace_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            ws_dir = temp_path / 'my_workspace'
            layout = WorkspaceLayout(ws_dir)
            layout.initialize()

            # 1. Environment variable takes precedence
            with patch.dict('os.environ', {'ROS_MAINTAINER_WS': str(ws_dir)}):
                self.assertEqual(get_default_workspace_path(), ws_dir)

            # 2. When env var is unset and inside workspace tree, searches upward
            nested_dir = ws_dir / 'sessions' / 'session-pr-1' / 'src' / 'pkg'
            nested_dir.mkdir(parents=True, exist_ok=True)
            with patch.dict('os.environ', {}, clear=True):
                with patch('pathlib.Path.cwd', return_value=nested_dir):
                    self.assertEqual(get_default_workspace_path(), ws_dir)

            # 3. When outside workspace and ~/ros_maintenance_ws is initialized, falls back to it
            outside_dir = temp_path / 'outside'
            outside_dir.mkdir()
            fake_home = temp_path / 'home'
            home_ws = fake_home / 'ros_maintenance_ws'
            WorkspaceLayout(home_ws).initialize()
            with patch.dict('os.environ', {}, clear=True):
                with patch('pathlib.Path.cwd', return_value=outside_dir):
                    with patch('pathlib.Path.home', return_value=fake_home):
                        self.assertEqual(get_default_workspace_path(), home_ws.resolve())

            # 4. When outside workspace and ~/ros_maintenance_ws is not initialized, defaults to cwd
            empty_home = temp_path / 'empty_home'
            empty_home.mkdir()
            with patch.dict('os.environ', {}, clear=True):
                with patch('pathlib.Path.cwd', return_value=outside_dir):
                    with patch('pathlib.Path.home', return_value=empty_home):
                        self.assertEqual(get_default_workspace_path(), outside_dir)

    def test_cli_token_doctor_and_container_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = str(Path(temp_dir) / 'cli_ws')

            # 1. token-setup --no-token
            ts_args = ['ros-maintainer-harness', '-w', ws_root, 'token-setup', '--no-token']
            with patch.object(sys, 'argv', ts_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('ROS_CONTAINER_GITHUB_TOKEN=none', fake_out.getvalue())

            # 2. doctor (with docker mocked)
            with patch('ros_maintainer_agent_harness.devcontainer.detect_container_runtime', return_value='docker'):
                with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'doctor']):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('Environment is ready', fake_out.getvalue())

            # 3. Create session and test session up / exec / down
            create_args = ['ros-maintainer-harness', '-w', ws_root, 'session', 'create', 'sess-1']
            with patch.object(sys, 'argv', create_args):
                with patch('sys.stdout', new=io.StringIO()):
                    main()

            sess_dir = Path(ws_root) / 'sessions' / 'sess-1'
            self.assertTrue((sess_dir / 'AGENTS.md').is_file())
            self.assertFalse((sess_dir / 'GEMINI.md').exists())
            self.assertTrue((sess_dir / 'CLAUDE.md').is_file())
            self.assertEqual((sess_dir / 'CLAUDE.md').read_text(encoding='utf-8').strip(), '@AGENTS.md')

            with patch('ros_maintainer_agent_harness.cli.start_session_container') as mock_up:
                mock_up.return_value = {
                    'success': True,
                    'status': 'started',
                    'container_name': 'ros-harness-sess-1',
                    'runtime': 'docker',
                }
                with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'session', 'up', 'sess-1']):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('ros-harness-sess-1', fake_out.getvalue())

            with patch('ros_maintainer_agent_harness.cli.exec_in_session_container') as mock_exec:
                mock_exec.return_value = {
                    'success': True,
                    'returncode': 0,
                    'stdout': 'colcon build done\n',
                    'stderr': '',
                }
                exec_args = [
                    'ros-maintainer-harness', '-w', ws_root, 'session', 'exec', 'sess-1', '--', 'colcon build'
                ]
                with patch.object(sys, 'argv', exec_args):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('colcon build done', fake_out.getvalue())

            with patch('ros_maintainer_agent_harness.cli.stop_session_container') as mock_down:
                mock_down.return_value = {'success': True, 'container_name': 'ros-harness-sess-1'}
                with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'session', 'down', 'sess-1']):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('Stopped and removed container', fake_out.getvalue())

    def test_workspace_flag_before_or_after_subcommands(self):
        from ros_maintainer_agent_harness.cli import parse_args

        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = str(Path(temp_dir) / 'ws_order')

            # 1. `ros-maintainer-harness init -w <path>` (repro from issue #28)
            with patch.object(sys, 'argv', ['ros-maintainer-harness', 'init', '-w', ws_root]):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Successfully initialized maintainer workspace', fake_out.getvalue())

            # 2. `status` with -w after subcommand
            with patch.object(sys, 'argv', ['ros-maintainer-harness', 'status', '--no-containers', '-w', ws_root]):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Maintainer Hub Status', fake_out.getvalue())

            # 3. `session create` with -w after nested subcommand
            with patch.object(sys, 'argv', ['ros-maintainer-harness', 'session', 'create', 'sess-w', '-w', ws_root]):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Created session 'sess-w'", fake_out.getvalue())

            # 4. `session -w <path> list` (-w between parent and nested subcommand)
            with patch.object(sys, 'argv', ['ros-maintainer-harness', 'session', '-w', ws_root, 'list']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('sess-w', fake_out.getvalue())

            # 5. `session up sess-w -w <path>`
            with patch('ros_maintainer_agent_harness.cli.start_session_container') as mock_up:
                mock_up.return_value = {
                    'success': True,
                    'status': 'started',
                    'container_name': 'ros-harness-sess-w',
                    'runtime': 'docker',
                }
                with patch.object(sys, 'argv', ['ros-maintainer-harness', 'session', 'up', 'sess-w', '-w', ws_root]):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('ros-harness-sess-w', fake_out.getvalue())

            # 6. `session exec sess-w -w <path> -- colcon build`
            with patch('ros_maintainer_agent_harness.cli.exec_in_session_container') as mock_exec:
                mock_exec.return_value = {
                    'success': True,
                    'returncode': 0,
                    'stdout': 'ok\n',
                    'stderr': '',
                }
                exec_argv = [
                    'ros-maintainer-harness', 'session', 'exec', 'sess-w', '-w', ws_root, '--', 'colcon build'
                ]
                with patch.object(sys, 'argv', exec_argv):
                    with patch('sys.stdout', new=io.StringIO()) as fake_out:
                        ret = main()
                        self.assertEqual(ret, 0)
                        self.assertIn('ok', fake_out.getvalue())

            # 7. `parse_args` checks for `release push` and `policy check` with trailing -w
            parsed_rel = parse_args([
                'release', 'push', '-s', 'sess-w', '-t', '1.2.3', '-m', 'release', '-w', ws_root,
            ])
            self.assertEqual(parsed_rel.workspace, ws_root)
            self.assertEqual(parsed_rel.release_action, 'push')

            parsed_override = parse_args(['-w', '/earlier', 'init', '-w', ws_root])
            self.assertEqual(parsed_override.workspace, ws_root)


if __name__ == '__main__':
    unittest.main()

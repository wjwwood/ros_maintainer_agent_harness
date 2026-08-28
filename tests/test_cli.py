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
from ros_maintainer_agent_harness.cli import main
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

            # 4. Test session list
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'session', 'list']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('session-pr-42', fake_out.getvalue())

            # 5. Test session prune
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


if __name__ == '__main__':
    unittest.main()

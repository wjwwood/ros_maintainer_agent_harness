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

from ros_maintainer_agent_harness.approval import ApprovalManager, ApprovalStatus
from ros_maintainer_agent_harness.audit import read_audit_records
from ros_maintainer_agent_harness.ci import CITracker
from ros_maintainer_agent_harness.git_ops import execute_git_push
from ros_maintainer_agent_harness.timeline import TimelineLogger
from ros_maintainer_agent_harness.workspace import WorkspaceLayout
from ros_maintainer_agent_harness.worktree import SessionManager


class TestEndToEndWorkflow(unittest.TestCase):
    """
    Comprehensive End-to-End integration test simulating a multi-agent ROS 2
    maintenance lifecycle across parallel sessions.
    """

    def test_full_multi_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'maintainer_ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            # 1. Verify workspace initialization
            self.assertTrue(layout.is_initialized())
            self.assertTrue((layout.tools_bin_dir / 'ros-find-restarted-ci').exists())
            self.assertTrue((layout.tools_bin_dir / 'ros-ci-for-pr').exists())
            self.assertTrue((layout.tools_bin_dir / 'ros-session-status').exists())

            # 2. Setup a shared Git repository
            shared_repo = layout.shared_repos_dir / 'ros2' / 'rclcpp'
            shared_repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'init', '-b', 'rolling'], cwd=str(shared_repo), check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=str(shared_repo), check=True)
            subprocess.run(['git', 'config', 'user.email', 'tester@example.com'], cwd=str(shared_repo), check=True)
            (shared_repo / 'README.md').write_text('# rclcpp\n', encoding='utf-8')
            (shared_repo / 'package.xml').write_text('<package><name>rclcpp</name></package>\n', encoding='utf-8')
            subprocess.run(['git', 'add', '.'], cwd=str(shared_repo), check=True, capture_output=True)
            subprocess.run(
                ['git', 'commit', '-m', 'Initial commit'],
                cwd=str(shared_repo),
                check=True,
                capture_output=True,
            )

            # Setup bare remote
            remote_repo = Path(temp_dir) / 'remote_rclcpp.git'
            subprocess.run(['git', 'init', '--bare', str(remote_repo)], check=True, capture_output=True)
            subprocess.run(['git', 'remote', 'add', 'origin', str(remote_repo)], cwd=str(shared_repo), check=True)

            # 3. Create Session A (PR 101, rolling) and Session B (PR 102, jazzy)
            session_mgr = SessionManager(layout)
            session_a = session_mgr.create_session('session-pr-101', topic='Fix rclcpp executor race', distro='rolling')
            session_b = session_mgr.create_session('session-pr-102', topic='Backport to jazzy', distro='jazzy')

            self.assertEqual(session_a.distro, 'rolling')
            self.assertEqual(session_b.distro, 'jazzy')

            # Verify devcontainer configs created
            self.assertTrue((session_a.session_dir / '.devcontainer' / 'devcontainer.json').exists())
            self.assertTrue((session_b.session_dir / '.devcontainer' / 'devcontainer.json').exists())

            # 4. Attach Git worktrees to each session
            wt_a = session_mgr.attach_worktree(
                session_id='session-pr-101',
                repo_dir=shared_repo,
                branch_name='wjwwood/fix_executor_race',
                base_ref='rolling',
            )
            wt_b = session_mgr.attach_worktree(
                session_id='session-pr-102',
                repo_dir=shared_repo,
                branch_name='wjwwood/backport_jazzy',
                base_ref='rolling',
            )
            self.assertTrue((wt_a / 'package.xml').exists())
            self.assertTrue((wt_b / 'package.xml').exists())

            # 5. Simulate code changes and local commit in Session A
            (wt_a / 'executor.cpp').write_text('// Fixed race condition\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'executor.cpp'], cwd=str(wt_a), check=True, capture_output=True)
            subprocess.run(
                ['git', 'commit', '-m', 'Fix race condition in executor'],
                cwd=str(wt_a),
                check=True,
                capture_output=True,
            )

            # 6. Log milestones to Session A timeline
            timeline_a = TimelineLogger('session-pr-101', session_a.session_dir, layout.audit_log_path)
            timeline_a.log_status('Fixed race condition in executor.cpp and compiled successfully.')
            timeline_a.log_milestone('Tests Passing', 'Ran colcon test and verified all tests pass.')

            # 7. CI Rate Limiting / Tracking
            ci_tracker = CITracker(layout.ci_runs_path)
            policy = layout.get_policy()
            allowed, msg, req_appr = policy.validate_jenkins_ci(
                pr_url='ros2/rclcpp#101',
                active_runs_count=ci_tracker.get_active_runs_count('ros2/rclcpp#101'),
                seconds_since_last_run=ci_tracker.get_seconds_since_last_run('ros2/rclcpp#101'),
            )
            self.assertTrue(allowed)
            self.assertFalse(req_appr)
            ci_tracker.record_run(
                'ros2/rclcpp#101',
                'session-pr-101',
                'ci_launcher',
                1,
                'https://ci.ros2.org/job/1',
                'RUNNING',
            )

            # Immediate second launch should be blocked by concurrency limit
            allowed2, msg2, req_appr2 = policy.validate_jenkins_ci(
                pr_url='ros2/rclcpp#101',
                active_runs_count=ci_tracker.get_active_runs_count('ros2/rclcpp#101'),
                seconds_since_last_run=ci_tracker.get_seconds_since_last_run('ros2/rclcpp#101'),
            )
            self.assertFalse(allowed2)
            self.assertFalse(req_appr2)
            self.assertIn('reached maximum limit', msg2)

            # 8. Approval Ticket System for PR Creation
            approval_mgr = ApprovalManager(layout.approvals_path)
            req = approval_mgr.create_request(
                session_id='session-pr-101',
                action='create_pull_request',
                target='ros2/rclcpp:wjwwood/fix_executor_race->rolling',
                reason='Propose fix for review',
            )
            self.assertEqual(req.status, ApprovalStatus.PENDING)
            self.assertFalse(approval_mgr.is_approved(req.ticket_id))

            # Maintainer approves ticket
            approval_mgr.approve_request(req.ticket_id, maintainer='wjwwood', comment='Looks good')
            self.assertTrue(approval_mgr.is_approved(req.ticket_id))

            # 9. Policy-Guarded Git Push Execution
            push_ok, push_msg = execute_git_push(
                repo_dir=wt_a,
                branch='wjwwood/fix_executor_race',
                remote='origin',
                force_with_lease=True,
                dry_run=False,
            )
            self.assertTrue(push_ok)

            # Log push to timeline & audit
            timeline_a.log_action(
                action='git_push',
                target='origin:wjwwood/fix_executor_race',
                reason='Pushed verified fix to remote fork',
                status='SUCCESS',
                details={'message': push_msg},
            )

            # 10. Audit Log Inspection
            records = read_audit_records(layout.audit_log_path)
            self.assertGreaterEqual(len(records), 1)
            push_records = [r for r in records if r['action'] == 'git_push']
            self.assertEqual(len(push_records), 1)
            self.assertEqual(push_records[0]['reason'], 'Pushed verified fix to remote fork')
            self.assertEqual(push_records[0]['status'], 'SUCCESS')

            # 11. Verify Timeline contents
            timeline_content = (session_a.session_dir / 'timeline.md').read_text(encoding='utf-8')
            self.assertIn('Fixed race condition in executor.cpp', timeline_content)
            self.assertIn('🏆 **Milestone**: Tests Passing', timeline_content)
            self.assertIn('Pushed verified fix to remote fork', timeline_content)

            # 12. Prune Session A; Verify Session B remains fully intact
            self.assertTrue(session_mgr.prune_session('session-pr-101'))
            self.assertFalse(session_a.session_dir.exists())
            self.assertTrue(session_b.session_dir.exists())
            self.assertTrue((wt_b / 'package.xml').exists())


if __name__ == '__main__':
    unittest.main()
